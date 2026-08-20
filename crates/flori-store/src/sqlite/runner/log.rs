use std::fmt::Write;

use flori_core::{
    AttemptId, CompiledTaskSpec, ErrorCode, LogCursor, LogFrame, RunnerId, TaskLogEvent,
    TaskLogLine,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    poll::server_log::{self, ServerLogUpload},
};

const MAX_LOG_LINE_BYTES: usize = 64 * 1024;

impl Store {
    pub async fn append_log_frames(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        frames: &[LogFrame],
        now_ms: i64,
    ) -> Result<LogCursor, StoreError> {
        if now_ms < 0 || frames.is_empty() {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut cursor = 0;
        for frame in frames {
            cursor = self
                .append_log_frame(artifacts, runner_id, attempt_id, frame, now_ms)
                .await?;
        }
        Ok(LogCursor {
            last_sequence: cursor,
        })
    }

    async fn append_log_frame(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        frame: &LogFrame,
        now_ms: i64,
    ) -> Result<u64, StoreError> {
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = active_log_attempt(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let spec: CompiledTaskSpec =
            serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
        let job_id: String = row.try_get("job_id")?;
        let task_id: String = row.try_get("task_id")?;
        let source_id = row
            .try_get::<String, _>("source_id")?
            .parse()
            .map_err(|_| corrupt())?;
        let events = load_events(&mut transaction, &job_id, attempt_id).await?;
        let cursor =
            u64::try_from(row.try_get::<i64, _>("last_log_sequence")?).map_err(|_| corrupt())?;
        validate_event_history(&events, cursor)?;
        let credential_value = source_credential(&mut transaction, &job_id).await?;
        validate_frame(frame, credential_value.as_deref())?;
        let pending = server_log::load_pending(
            &mut transaction,
            source_id,
            &job_id,
            &task_id,
            attempt_id,
            &spec,
        )
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::ArtifactUndeclared))?;
        if frame.sequence <= cursor {
            let Some(existing) = events
                .iter()
                .find(|event| event.frame.sequence == frame.sequence)
            else {
                return Err(corrupt());
            };
            if existing.frame != *frame {
                return Err(StoreError::new(ErrorCode::LogSequenceConflict));
            }
            verify_current_log(artifacts, &pending, &events)?;
            transaction.rollback().await?;
            return Ok(cursor);
        }
        if frame.sequence != cursor + 1 {
            return Err(StoreError::new(ErrorCode::LogSequenceGap));
        }
        let mut chunk = frame.line.as_bytes().to_vec();
        chunk.push(b'\n');
        let chunk_sha256 = server_log::sha256(&chunk)?;
        let received = artifacts
            .append_chunk(
                &pending.record,
                pending.record.received_bytes(),
                &chunk_sha256,
                &chunk,
            )
            .map_err(|error| StoreError::new(error.code()))?;
        let mut updated_events = events;
        let event = TaskLogEvent {
            exec_id: attempt_id,
            frame: frame.clone(),
        };
        updated_events.push(event.clone());
        let rolling_sha256 = server_log::sha256(&server_log::log_bytes(&updated_events)?)?;
        insert_event(&mut transaction, &job_id, &event, now_ms).await?;
        let updated = sqlx::query(
            "UPDATE attempts SET last_log_sequence=? WHERE id=? AND last_log_sequence=?",
        )
        .bind(i64::try_from(frame.sequence).map_err(|_| invalid())?)
        .bind(attempt_id.to_string())
        .bind(i64::try_from(cursor).map_err(|_| corrupt())?)
        .execute(&mut *transaction)
        .await?;
        ensure_one(updated, ErrorCode::StaleAttempt)?;
        let updated = sqlx::query(
            "UPDATE uploads SET received_bytes=?,expected_sha256=?,updated_at_ms=? \
             WHERE id=? AND commit_json IS NULL AND state='receiving' AND received_bytes=?",
        )
        .bind(i64::try_from(received).map_err(|_| invalid())?)
        .bind(rolling_sha256.as_str())
        .bind(now_ms)
        .bind(pending.upload_id.to_string())
        .bind(i64::try_from(pending.record.received_bytes()).map_err(|_| corrupt())?)
        .execute(&mut *transaction)
        .await?;
        ensure_one(updated, ErrorCode::Conflict)?;
        transaction.commit().await?;
        Ok(frame.sequence)
    }
}

async fn active_log_attempt(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<sqlx::sqlite::SqliteRow, StoreError> {
    let row = sqlx::query(
        "SELECT a.runner_id,a.state,a.lease_expires_at_ms,a.last_log_sequence, \
         t.id AS task_id,t.current_attempt_id,t.state AS task_state,t.spec_json, \
         j.id AS job_id,j.source_id,j.state AS job_state FROM attempts a JOIN tasks t \
         ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id WHERE a.id=?",
    )
    .bind(attempt_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
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
    Ok(row)
}

fn verify_current_log(
    artifacts: &NasArtifactStore,
    pending: &ServerLogUpload,
    events: &[TaskLogEvent],
) -> Result<(), StoreError> {
    let bytes = server_log::log_bytes(events)?;
    let digest = server_log::sha256(&bytes)?;
    if pending.record.received_bytes() != bytes.len() as u64 || pending.rolling_sha256 != digest {
        return Err(corrupt());
    }
    let received = artifacts
        .append_chunk(&pending.record, 0, &digest, &bytes)
        .map_err(|error| StoreError::new(error.code()))?;
    if received == pending.record.received_bytes() {
        Ok(())
    } else {
        Err(corrupt())
    }
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
        .map(|payload| serde_json::from_str::<TaskLogEvent>(&payload).map_err(|_| corrupt()))
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
    if frame.line.len() > MAX_LOG_LINE_BYTES
        || serde_json::from_str::<TaskLogLine>(&frame.line).is_err()
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
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

async fn insert_event(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    job_id: &str,
    event: &TaskLogEvent,
    now_ms: i64,
) -> Result<(), StoreError> {
    sqlx::query(
        "INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) \
         VALUES('job',?,'log_cursor',?,?)",
    )
    .bind(job_id)
    .bind(serde_json::to_string(event).map_err(|_| corrupt())?)
    .bind(now_ms)
    .execute(&mut **transaction)
    .await?;
    Ok(())
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

fn ensure_one(result: sqlx::sqlite::SqliteQueryResult, code: ErrorCode) -> Result<(), StoreError> {
    if result.rows_affected() == 1 {
        Ok(())
    } else {
        Err(StoreError::new(code))
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
