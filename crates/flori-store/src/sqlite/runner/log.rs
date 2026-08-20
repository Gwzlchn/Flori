use std::fmt::Write;

use flori_core::{
    ArtifactKind, AttemptId, CompiledTaskSpec, ErrorCode, LogCursor, LogFrame, RunnerId,
    TaskLogEvent,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use super::super::{Store, StoreError};

const MAX_LOG_LINE_BYTES: usize = 64 * 1024;

impl Store {
    pub async fn append_log_frames(
        &self,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        frames: &[LogFrame],
        now_ms: i64,
    ) -> Result<LogCursor, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let attempt_id_text = attempt_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            "SELECT a.runner_id,a.state,a.lease_expires_at_ms,a.last_log_sequence, \
             t.current_attempt_id,t.state AS task_state,t.spec_json,j.id AS job_id, \
             j.state AS job_state FROM attempts a JOIN tasks t ON t.id=a.task_id \
             JOIN jobs j ON j.id=t.job_id WHERE a.id=?",
        )
        .bind(&attempt_id_text)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        };
        validate_active(&row, runner_id, attempt_id, now_ms)?;
        let spec: CompiledTaskSpec = serde_json::from_str(row.try_get("spec_json")?)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        let limits = spec
            .artifacts
            .iter()
            .filter(|artifact| artifact.kind == ArtifactKind::TaskLog)
            .map(|artifact| artifact.max_bytes)
            .collect::<Vec<_>>();
        if limits.len() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::ArtifactUndeclared));
        }
        let max_bytes = limits[0];
        let job_id: String = row.try_get("job_id")?;
        let events = load_events(&mut transaction, &job_id, attempt_id).await?;
        let mut total_bytes = events.iter().try_fold(0_u64, |total, event| {
            total
                .checked_add(encoded_frame_bytes(&event.frame)?)
                .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))
        })?;
        let mut cursor = u64::try_from(row.try_get::<i64, _>("last_log_sequence")?)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        validate_event_history(&events, cursor)?;
        let credential_value = source_credential(&mut transaction, &job_id).await?;

        for frame in frames {
            validate_frame(frame, credential_value.as_deref())?;
            if frame.sequence <= cursor {
                let Some(existing) = events
                    .iter()
                    .find(|event| event.frame.sequence == frame.sequence)
                else {
                    return Err(StoreError::new(ErrorCode::CorruptState));
                };
                if existing.frame != *frame {
                    return Err(StoreError::new(ErrorCode::LogSequenceConflict));
                }
                continue;
            }
            if frame.sequence != cursor + 1 {
                return Err(StoreError::new(ErrorCode::LogSequenceGap));
            }
            total_bytes = total_bytes
                .checked_add(encoded_frame_bytes(frame)?)
                .ok_or_else(|| StoreError::new(ErrorCode::ArtifactTooLarge))?;
            if total_bytes > max_bytes {
                return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
            }
            let payload = serde_json::to_string(&TaskLogEvent {
                exec_id: attempt_id,
                frame: frame.clone(),
            })
            .map_err(|_| StoreError::new(ErrorCode::Internal))?;
            sqlx::query(
                "INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) \
                 VALUES('job',?,'log_cursor',?,?)",
            )
            .bind(&job_id)
            .bind(payload)
            .bind(now_ms)
            .execute(&mut *transaction)
            .await?;
            cursor = frame.sequence;
        }
        let updated = sqlx::query(
            "UPDATE attempts SET last_log_sequence=? WHERE id=? AND last_log_sequence<=?",
        )
        .bind(i64::try_from(cursor).map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?)
        .bind(&attempt_id_text)
        .bind(i64::try_from(cursor).map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?)
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        transaction.commit().await?;
        Ok(LogCursor {
            last_sequence: cursor,
        })
    }
}

fn validate_active(
    row: &sqlx::sqlite::SqliteRow,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<(), StoreError> {
    let current: Option<String> = row.try_get("current_attempt_id")?;
    if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
        != Some(runner_id.to_string().as_str())
        || row.try_get::<String, _>("state")? != "leased"
        || row.try_get::<String, _>("task_state")? != "leased"
        || row.try_get::<String, _>("job_state")? != "running"
        || current.as_deref() != Some(attempt_id.to_string().as_str())
    {
        return Err(StoreError::new(ErrorCode::StaleAttempt));
    }
    if row.try_get::<i64, _>("lease_expires_at_ms")? <= now_ms {
        return Err(StoreError::new(ErrorCode::LeaseExpired));
    }
    Ok(())
}

async fn load_events(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
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
        .map(|payload| {
            serde_json::from_str::<TaskLogEvent>(&payload)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))
        })
        .filter(|event| match event {
            Ok(event) => event.exec_id == attempt_id,
            Err(_) => true,
        })
        .collect()
}

fn validate_event_history(events: &[TaskLogEvent], cursor: u64) -> Result<(), StoreError> {
    if events.len() != usize::try_from(cursor).map_err(|_| corrupt())?
        || events
            .iter()
            .enumerate()
            .any(|(index, event)| event.frame.sequence != index as u64 + 1)
    {
        return Err(corrupt());
    }
    Ok(())
}

fn validate_frame(frame: &LogFrame, credential_value: Option<&str>) -> Result<(), StoreError> {
    if frame.line.len() > MAX_LOG_LINE_BYTES {
        return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
    }
    let mut actual = String::with_capacity(64);
    for byte in Sha256::digest(frame.line.as_bytes()) {
        write!(&mut actual, "{byte:02x}").expect("writing to String cannot fail");
    }
    if actual != frame.sha256.as_str() {
        return Err(StoreError::new(ErrorCode::DigestMismatch));
    }
    if let Some(value) = credential_value
        && !value.is_empty()
    {
        let encoded = serde_json::to_string(value).map_err(|_| corrupt())?;
        let escaped = encoded
            .strip_prefix('"')
            .and_then(|value| value.strip_suffix('"'))
            .ok_or_else(corrupt)?;
        if frame.line.contains(value) || frame.line.contains(escaped) {
            return Err(StoreError::new(ErrorCode::CredentialUnavailable));
        }
    }
    Ok(())
}

fn encoded_frame_bytes(frame: &LogFrame) -> Result<u64, StoreError> {
    let bytes = serde_json::to_vec(frame)
        .map_err(|_| corrupt())?
        .len()
        .checked_add(1)
        .ok_or_else(corrupt)?;
    u64::try_from(bytes).map_err(|_| corrupt())
}

async fn source_credential(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    job_id: &str,
) -> Result<Option<String>, StoreError> {
    sqlx::query_scalar(
        "SELECT c.plaintext_value FROM jobs j JOIN sources s ON s.id=j.source_id \
         JOIN credentials c ON c.id=s.credential_id WHERE j.id=?",
    )
    .bind(job_id)
    .fetch_optional(&mut **transaction)
    .await
    .map_err(Into::into)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
