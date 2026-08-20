use std::fmt::Write;

use flori_core::{
    AttemptId, CompiledTaskSpec, ErrorCode, LogCursor, LogFrame, RunnerId, TaskLogEvent,
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
        let cursor =
            u64::try_from(row.try_get::<i64, _>("last_log_sequence")?).map_err(|_| corrupt())?;
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
        let mut committed = committed_bytes(artifacts, &pending, cursor)?;
        if frame.sequence <= cursor {
            if log_line(&committed, frame.sequence)? != frame.line {
                return Err(StoreError::new(ErrorCode::LogSequenceConflict));
            }
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
        committed.extend_from_slice(&chunk);
        let rolling_sha256 = server_log::sha256(&committed)?;
        let event = TaskLogEvent {
            job_id: job_id.parse().map_err(|_| corrupt())?,
            task_id: task_id.parse().map_err(|_| corrupt())?,
            attempt_id,
            last_sequence: frame.sequence,
        };
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

fn committed_bytes(
    artifacts: &NasArtifactStore,
    pending: &ServerLogUpload,
    expected_lines: u64,
) -> Result<Vec<u8>, StoreError> {
    let size = pending.record.received_bytes();
    let bytes = if size == 0 {
        Vec::new()
    } else {
        let relative_path = pending.record.staging_relative_path();
        let path = relative_path.to_str().ok_or_else(corrupt)?;
        artifacts
            .read_chunk(path, 0, usize::try_from(size).map_err(|_| corrupt())?)
            .map_err(|error| StoreError::new(error.code()))?
    };
    validate_log_bytes(&bytes, expected_lines, size, &pending.rolling_sha256)?;
    Ok(bytes)
}

fn validate_log_bytes(
    bytes: &[u8],
    expected_lines: u64,
    expected_size: u64,
    expected_sha256: &flori_core::Sha256Digest,
) -> Result<(), StoreError> {
    if bytes.len() as u64 != expected_size
        || &server_log::sha256(bytes)? != expected_sha256
        || (expected_lines == 0) != bytes.is_empty()
    {
        return Err(corrupt());
    }
    if bytes.is_empty() {
        return Ok(());
    }
    let text = std::str::from_utf8(bytes).map_err(|_| corrupt())?;
    let mut count = 0_u64;
    for line in text.strip_suffix('\n').ok_or_else(corrupt)?.split('\n') {
        if server_log::canonical_line(line).is_none() {
            return Err(corrupt());
        }
        count = count.checked_add(1).ok_or_else(corrupt)?;
    }
    if count != expected_lines {
        return Err(corrupt());
    }
    Ok(())
}

fn log_line(bytes: &[u8], sequence: u64) -> Result<&str, StoreError> {
    let index =
        usize::try_from(sequence.checked_sub(1).ok_or_else(corrupt)?).map_err(|_| corrupt())?;
    std::str::from_utf8(bytes)
        .map_err(|_| corrupt())?
        .strip_suffix('\n')
        .ok_or_else(corrupt)?
        .split('\n')
        .nth(index)
        .ok_or_else(corrupt)
}

fn validate_frame(frame: &LogFrame, credential_value: Option<&str>) -> Result<(), StoreError> {
    if frame.line.len() > MAX_LOG_LINE_BYTES {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    let line = server_log::canonical_line(&frame.line)
        .ok_or_else(|| StoreError::new(ErrorCode::InvalidRequest))?;
    let mut actual = String::with_capacity(64);
    for byte in Sha256::digest(frame.line.as_bytes()) {
        write!(&mut actual, "{byte:02x}").expect("writing to String cannot fail");
    }
    if actual != frame.sha256.as_str() {
        return Err(StoreError::new(ErrorCode::DigestMismatch));
    }
    if let Some(value) = credential_value
        && !value.is_empty()
        && line.message.contains(value)
    {
        return Err(StoreError::new(ErrorCode::CredentialUnavailable));
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
