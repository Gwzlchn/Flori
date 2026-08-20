use std::fmt::Write;

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptId, ErrorCode, PendingAttemptUpload, Sha256Digest,
    TaskLogEvent, TaskLogLine, UploadState,
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
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<(), StoreError> {
    let events = load_events(transaction, row.try_get("job_id")?, attempt_id).await?;
    let cursor: i64 = row.try_get("last_log_sequence")?;
    if u64::try_from(cursor).map_err(|_| corrupt())? != events.len() as u64 {
        return Err(corrupt());
    }
    let bytes = log_bytes(&events)?;
    let digest = digest(&bytes);
    if record.received_bytes() != bytes.len() as u64 || record.expected_sha256() != &digest {
        return Err(corrupt());
    }
    match artifacts.recovery_action(record, true) {
        Ok(RecoveryAction::ResumeReceiving) => validate_staging_prefix(artifacts, record, &bytes),
        Ok(_) => Err(corrupt()),
        Err(error) if error.code() != ErrorCode::CorruptState => Err(StoreError::new(error.code())),
        Err(_) if bytes.is_empty() => Err(corrupt()),
        Err(_) => {
            recover_final_only(transaction, artifacts, row, record, bytes, digest, now_ms).await
        }
    }
}

fn validate_staging_prefix(
    artifacts: &NasArtifactStore,
    record: &UploadRecord,
    bytes: &[u8],
) -> Result<(), StoreError> {
    if bytes.is_empty() {
        return Ok(());
    }
    let stored = artifacts
        .read_chunk(
            record
                .staging_relative_path()
                .to_str()
                .ok_or_else(corrupt)?,
            0,
            bytes.len(),
        )
        .map_err(|error| StoreError::new(error.code()))?;
    if stored == bytes {
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
    bytes: Vec<u8>,
    digest: Sha256Digest,
    now_ms: i64,
) -> Result<(), StoreError> {
    let upload_id = row
        .try_get::<String, _>("id")?
        .parse()
        .map_err(|_| corrupt())?;
    let name: String = row.try_get("name")?;
    let mut actual = UploadRecord::new(
        upload_id,
        &name,
        record.final_relative_path(),
        bytes.len() as u64,
        digest.clone(),
        &name,
        record.expected_size_bytes(),
    )
    .map_err(|_| corrupt())?;
    actual
        .restore_progress(bytes.len() as u64, UploadState::Verified)
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
            size_bytes: bytes.len() as u64,
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
    .bind(i64::try_from(bytes.len()).map_err(|_| corrupt())?)
    .bind(digest.as_str())
    .bind(now_ms)
    .bind(upload_id.to_string())
    .bind(i64::try_from(bytes.len()).map_err(|_| corrupt())?)
    .execute(&mut **transaction)
    .await?;
    if updated.rows_affected() == 1 {
        Ok(())
    } else {
        Err(StoreError::new(ErrorCode::Conflict))
    }
}

async fn load_events(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    attempt_id: AttemptId,
) -> Result<Vec<TaskLogEvent>, StoreError> {
    let payloads: Vec<String> = sqlx::query_scalar(
        "SELECT payload_json FROM job_events WHERE scope='job' AND scope_id=? \
         AND kind='log_cursor' ORDER BY id",
    )
    .bind(job_id)
    .fetch_all(&mut **transaction)
    .await?;
    payloads
        .into_iter()
        .map(|payload| serde_json::from_str::<TaskLogEvent>(&payload).map_err(|_| corrupt()))
        .filter(|event| match event {
            Ok(event) => event.exec_id == attempt_id,
            Err(_) => true,
        })
        .collect()
}

fn log_bytes(events: &[TaskLogEvent]) -> Result<Vec<u8>, StoreError> {
    let mut bytes = Vec::new();
    for (index, event) in events.iter().enumerate() {
        if event.frame.sequence != index as u64 + 1
            || serde_json::from_str::<TaskLogLine>(&event.frame.line).is_err()
            || digest(event.frame.line.as_bytes()) != event.frame.sha256
        {
            return Err(corrupt());
        }
        bytes.extend_from_slice(event.frame.line.as_bytes());
        bytes.push(b'\n');
    }
    Ok(bytes)
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
