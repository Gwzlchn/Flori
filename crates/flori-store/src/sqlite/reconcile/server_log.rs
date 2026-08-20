use std::fmt::Write;

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptId, ErrorCode, PendingAttemptUpload, Sha256Digest,
    TaskLogLine, UploadState,
};
use sha2::{Digest, Sha256};
use sqlx::{Row, Sqlite, Transaction, sqlite::SqliteRow};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use crate::sqlite::StoreError;

pub(super) async fn reconcile_open_log(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    row: &SqliteRow,
    record: &UploadRecord,
    _attempt_id: AttemptId,
    now_ms: i64,
) -> Result<(), StoreError> {
    let cursor =
        u64::try_from(row.try_get::<i64, _>("last_log_sequence")?).map_err(|_| corrupt())?;
    match artifacts.recovery_action(record, true) {
        Ok(RecoveryAction::ResumeReceiving) => {
            let bytes = read_exact(
                artifacts,
                record
                    .staging_relative_path()
                    .to_str()
                    .ok_or_else(corrupt)?,
                record.received_bytes(),
            )?;
            validate_bytes(&bytes, cursor, record.expected_sha256())
        }
        Ok(_) => Err(corrupt()),
        Err(error) if error.code() != ErrorCode::CorruptState => Err(StoreError::new(error.code())),
        Err(_) if record.received_bytes() == 0 => Err(corrupt()),
        Err(_) => {
            let bytes = read_exact(
                artifacts,
                record.final_relative_path(),
                record.received_bytes(),
            )?;
            validate_bytes(&bytes, cursor, record.expected_sha256())?;
            recover_final_only(transaction, artifacts, row, record, now_ms).await
        }
    }
}

fn read_exact(artifacts: &NasArtifactStore, path: &str, size: u64) -> Result<Vec<u8>, StoreError> {
    if size == 0 {
        return Ok(Vec::new());
    }
    let bytes = artifacts
        .read_chunk(path, 0, usize::try_from(size).map_err(|_| corrupt())?)
        .map_err(|error| StoreError::new(error.code()))?;
    if bytes.len() as u64 == size {
        Ok(bytes)
    } else {
        Err(corrupt())
    }
}

fn validate_bytes(
    bytes: &[u8],
    expected_lines: u64,
    expected_sha256: &Sha256Digest,
) -> Result<(), StoreError> {
    if &digest(bytes) != expected_sha256 || (expected_lines == 0) != bytes.is_empty() {
        return Err(corrupt());
    }
    if bytes.is_empty() {
        return Ok(());
    }
    let text = std::str::from_utf8(bytes).map_err(|_| corrupt())?;
    let mut count = 0_u64;
    for raw in text.strip_suffix('\n').ok_or_else(corrupt)?.split('\n') {
        let line = serde_json::from_str::<TaskLogLine>(raw).map_err(|_| corrupt())?;
        if serde_json::to_string(&line).map_err(|_| corrupt())? != raw {
            return Err(corrupt());
        }
        count = count.checked_add(1).ok_or_else(corrupt)?;
    }
    if count == expected_lines {
        Ok(())
    } else {
        Err(corrupt())
    }
}

async fn recover_final_only(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    row: &SqliteRow,
    record: &UploadRecord,
    now_ms: i64,
) -> Result<(), StoreError> {
    let upload_id = row
        .try_get::<String, _>("id")?
        .parse()
        .map_err(|_| corrupt())?;
    let name: String = row.try_get("name")?;
    let digest = record.expected_sha256().clone();
    let mut actual = UploadRecord::new(
        upload_id,
        &name,
        record.final_relative_path(),
        record.received_bytes(),
        digest.clone(),
        &name,
        record.expected_size_bytes(),
    )
    .map_err(|_| corrupt())?;
    actual
        .restore_progress(record.received_bytes(), UploadState::Verified)
        .map_err(|_| corrupt())?;
    if artifacts
        .recovery_action(&actual, true)
        .map_err(|error| StoreError::new(error.code()))?
        != RecoveryAction::MarkMoved
    {
        return Err(corrupt());
    }
    let commit = PendingAttemptUpload {
        artifact_id: row
            .try_get::<String, _>("target_id")?
            .parse()
            .map_err(|_| corrupt())?,
        declaration_name: name.clone(),
        artifact: ArtifactManifestEntry {
            name,
            kind: ArtifactKind::TaskLog,
            media_type: "application/x-ndjson".to_owned(),
            size_bytes: record.received_bytes(),
            sha256: digest.clone(),
            relative_path: record.final_relative_path().to_owned(),
        },
    };
    let updated = sqlx::query(
        "UPDATE uploads SET commit_json=?,expected_size_bytes=?,expected_sha256=?,state='moved', \
         updated_at_ms=? WHERE id=? AND commit_json IS NULL AND state='receiving' \
         AND received_bytes=?",
    )
    .bind(serde_json::to_string(&commit).map_err(|_| corrupt())?)
    .bind(i64::try_from(record.received_bytes()).map_err(|_| corrupt())?)
    .bind(digest.as_str())
    .bind(now_ms)
    .bind(upload_id.to_string())
    .bind(i64::try_from(record.received_bytes()).map_err(|_| corrupt())?)
    .execute(&mut **transaction)
    .await?;
    if updated.rows_affected() == 1 {
        Ok(())
    } else {
        Err(StoreError::new(ErrorCode::Conflict))
    }
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(value).expect("SHA-256 formatter is canonical")
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
