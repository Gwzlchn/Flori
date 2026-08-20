use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptId, ErrorCode, PendingAttemptUpload, TaskLogEvent,
    UploadState,
};
use sqlx::{Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, UploadRecord};

use super::super::super::super::StoreError;
use super::super::super::upload::ActiveAttempt;
use super::{load_pending, log_bytes, sha256};

pub(in crate::sqlite::runner) async fn finalize(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    active: &ActiveAttempt,
    attempt_id: AttemptId,
    failure: bool,
    now_ms: i64,
) -> Result<(), StoreError> {
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
    let events = load_events(transaction, &active.job_id.to_string(), attempt_id).await?;
    if events.is_empty() {
        return if failure {
            Ok(())
        } else {
            Err(StoreError::new(ErrorCode::ArtifactUndeclared))
        };
    }
    let bytes = log_bytes(&events)?;
    let digest = sha256(&bytes)?;
    if pending.record.received_bytes() != bytes.len() as u64 || pending.rolling_sha256 != digest {
        return Err(corrupt());
    }
    let mut actual = UploadRecord::new(
        pending.upload_id,
        &pending.declaration_name,
        pending.record.final_relative_path(),
        bytes.len() as u64,
        digest.clone(),
        &pending.declaration_name,
        pending.record.expected_size_bytes(),
    )
    .map_err(|_| corrupt())?;
    actual
        .restore_progress(bytes.len() as u64, UploadState::Verified)
        .map_err(|_| corrupt())?;
    artifacts
        .move_verified(&actual)
        .map_err(|error| StoreError::new(error.code()))?;
    let artifact = ArtifactManifestEntry {
        name: pending.declaration_name.clone(),
        kind: ArtifactKind::TaskLog,
        media_type: "application/x-ndjson".to_owned(),
        size_bytes: bytes.len() as u64,
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
    .bind(i64::try_from(bytes.len()).map_err(|_| corrupt())?)
    .bind(digest.as_str())
    .bind(now_ms)
    .bind(pending.upload_id.to_string())
    .bind(i64::try_from(bytes.len()).map_err(|_| corrupt())?)
    .execute(&mut **transaction)
    .await?;
    if updated.rows_affected() != 1 {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    Ok(())
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

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
