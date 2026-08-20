use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptId, ErrorCode, PendingAttemptUpload, UploadState,
};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::super::super::super::StoreError;
use super::super::super::upload::ActiveAttempt;
use super::{canonical_line, load_pending, sha256, task_log_declaration};

pub(in crate::sqlite::runner) async fn finalize(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    active: &ActiveAttempt,
    attempt_id: AttemptId,
    failure: bool,
    now_ms: i64,
) -> Result<(), StoreError> {
    let existing = if let Some(declared) = task_log_declaration(&active.spec)? {
        sqlx::query(
            "SELECT commit_json,state FROM uploads WHERE owner_kind='attempt' AND owner_id=? \
             AND name=?",
        )
        .bind(attempt_id.to_string())
        .bind(&declared.name)
        .fetch_optional(&mut **transaction)
        .await?
    } else {
        None
    };
    if let Some(row) = existing
        && row.try_get::<Option<String>, _>("commit_json")?.is_some()
    {
        return if row.try_get::<String, _>("state")? == "moved" {
            Ok(())
        } else {
            Err(corrupt())
        };
    }
    let Some(pending) = load_pending(
        transaction,
        active.source_id,
        &active.job_id.to_string(),
        &active.task_id.to_string(),
        attempt_id,
        &active.spec,
    )
    .await?
    else {
        return Ok(());
    };
    let cursor: i64 = sqlx::query_scalar("SELECT last_log_sequence FROM attempts WHERE id=?")
        .bind(attempt_id.to_string())
        .fetch_one(&mut **transaction)
        .await?;
    let cursor = u64::try_from(cursor).map_err(|_| corrupt())?;
    if cursor == 0 {
        return if failure {
            Ok(())
        } else {
            Err(StoreError::new(ErrorCode::ArtifactUndeclared))
        };
    }
    let digest = pending.rolling_sha256.clone();
    let mut actual = UploadRecord::new(
        pending.upload_id,
        &pending.declaration_name,
        pending.record.final_relative_path(),
        pending.record.received_bytes(),
        digest.clone(),
        &pending.declaration_name,
        pending.record.expected_size_bytes(),
    )
    .map_err(|_| corrupt())?;
    actual
        .restore_progress(pending.record.received_bytes(), UploadState::Verified)
        .map_err(|_| corrupt())?;
    let action = artifacts
        .recovery_action(&actual, true)
        .map_err(|error| StoreError::new(error.code()))?;
    let path = match action {
        RecoveryAction::MoveVerified => actual
            .staging_relative_path()
            .to_string_lossy()
            .into_owned(),
        RecoveryAction::MarkMoved => actual.final_relative_path().to_owned(),
        _ => return Err(corrupt()),
    };
    let bytes = read_exact(artifacts, &path, actual.expected_size_bytes())?;
    validate_bytes(&bytes, cursor, &digest)?;
    artifacts
        .move_verified(&actual)
        .map_err(|error| StoreError::new(error.code()))?;
    let artifact = ArtifactManifestEntry {
        name: pending.declaration_name.clone(),
        kind: ArtifactKind::TaskLog,
        media_type: "application/x-ndjson".to_owned(),
        size_bytes: actual.expected_size_bytes(),
        sha256: digest.clone(),
        relative_path: pending.record.final_relative_path().to_owned(),
    };
    let commit = PendingAttemptUpload {
        artifact_id: pending.artifact_id,
        declaration_name: pending.declaration_name,
        artifact,
    };
    let updated = sqlx::query(
        "UPDATE uploads SET commit_json=?,expected_size_bytes=?,expected_sha256=?,state='moved', \
         updated_at_ms=? WHERE id=? AND commit_json IS NULL AND state='receiving' AND received_bytes=?",
    )
    .bind(serde_json::to_string(&commit).map_err(|_| corrupt())?)
    .bind(i64::try_from(actual.expected_size_bytes()).map_err(|_| corrupt())?)
    .bind(digest.as_str())
    .bind(now_ms)
    .bind(pending.upload_id.to_string())
    .bind(i64::try_from(actual.expected_size_bytes()).map_err(|_| corrupt())?)
    .execute(&mut **transaction)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    Ok(())
}

fn read_exact(artifacts: &NasArtifactStore, path: &str, size: u64) -> Result<Vec<u8>, StoreError> {
    if size == 0 {
        return Err(corrupt());
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
    expected_sha256: &flori_core::Sha256Digest,
) -> Result<(), StoreError> {
    if &sha256(bytes)? != expected_sha256 {
        return Err(corrupt());
    }
    let text = std::str::from_utf8(bytes).map_err(|_| corrupt())?;
    let mut count = 0_u64;
    for line in text.strip_suffix('\n').ok_or_else(corrupt)?.split('\n') {
        if canonical_line(line).is_none() {
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

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
