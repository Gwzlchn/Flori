use std::fmt::Write;

use flori_core::{
    ArtifactId, ArtifactKind, AttemptId, CompiledTaskSpec, ErrorCode, Sha256Digest, SourceId,
    TaskLogEvent, TaskLogLine, UploadId, UploadState,
};
use sha2::{Digest, Sha256};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{UploadRecord, task_artifact_path};

use super::super::super::StoreError;
use super::super::upload_rule::declaration;
use super::ExecutionSelection;

mod finalize;
pub(in crate::sqlite::runner) use finalize::finalize;

pub(in crate::sqlite::runner) struct ServerLogUpload {
    pub upload_id: UploadId,
    pub record: UploadRecord,
    pub artifact_id: ArtifactId,
    pub declaration_name: String,
    pub rolling_sha256: Sha256Digest,
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn start_attempt(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: SourceId,
    job_id: &str,
    task_id: &str,
    runner_id: &str,
    exec_id: AttemptId,
    attempt_no: u8,
    selection: &ExecutionSelection,
    spec: &CompiledTaskSpec,
    now_ms: i64,
    lease_expires_at_ms: i64,
) -> Result<(), StoreError> {
    let started = sqlx::query(
        "UPDATE jobs SET state='running',started_at_ms=COALESCE(started_at_ms,?) \
         WHERE id=? AND state IN ('queued','running')",
    )
    .bind(now_ms)
    .bind(job_id)
    .execute(&mut **transaction)
    .await?;
    ensure_one(started)?;
    sqlx::query(
        "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,model,effort, \
         runner_config_revision,lease_expires_at_ms,last_log_sequence,started_at_ms) \
         VALUES(?,?,?,?,'leased',?,?,?,?,0,?)",
    )
    .bind(exec_id.to_string())
    .bind(task_id)
    .bind(i64::from(attempt_no))
    .bind(runner_id)
    .bind(&selection.model)
    .bind(&selection.effort)
    .bind(i64::try_from(selection.config_revision).map_err(|_| corrupt())?)
    .bind(lease_expires_at_ms)
    .bind(now_ms)
    .execute(&mut **transaction)
    .await?;
    let leased = sqlx::query(
        "UPDATE tasks SET state='leased',current_attempt_id=?,started_at_ms=COALESCE(started_at_ms,?) \
         WHERE id=? AND state='ready' AND job_id=?",
    )
    .bind(exec_id.to_string())
    .bind(now_ms)
    .bind(task_id)
    .bind(job_id)
    .execute(&mut **transaction)
    .await?;
    ensure_one(leased)?;
    create_log_ledger(
        transaction,
        source_id,
        job_id,
        task_id,
        exec_id,
        spec,
        now_ms,
    )
    .await
}

pub(in crate::sqlite::runner) async fn load_pending(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: SourceId,
    job_id: &str,
    task_id: &str,
    attempt_id: AttemptId,
    spec: &CompiledTaskSpec,
) -> Result<Option<ServerLogUpload>, StoreError> {
    let Some(declared) = task_log_declaration(spec)? else {
        return Ok(None);
    };
    let row = sqlx::query(
        "SELECT id,commit_json,name,target_id,staging_path,final_relative_path, \
         expected_size_bytes,expected_sha256,received_bytes,state FROM uploads \
         WHERE owner_kind='attempt' AND owner_id=? AND name=?",
    )
    .bind(attempt_id.to_string())
    .bind(&declared.name)
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(corrupt)?;
    if row.try_get::<Option<String>, _>("commit_json")?.is_some()
        || row.try_get::<String, _>("name")? != declared.name
        || row.try_get::<String, _>("state")? != "receiving"
        || row.try_get::<i64, _>("expected_size_bytes")?
            != i64::try_from(declared.max_bytes).map_err(|_| corrupt())?
    {
        return Err(corrupt());
    }
    let upload_id: UploadId = row
        .try_get::<String, _>("id")?
        .parse()
        .map_err(|_| corrupt())?;
    let artifact_id: ArtifactId = row
        .try_get::<String, _>("target_id")?
        .parse()
        .map_err(|_| corrupt())?;
    let (_, basename) = declaration(spec, &declared.name)?;
    let final_path = task_artifact_path(
        source_id,
        job_id.parse().map_err(|_| corrupt())?,
        task_id.parse().map_err(|_| corrupt())?,
        artifact_id,
        &basename,
    )
    .map_err(|error| StoreError::new(error.code()))?;
    let rolling_sha256 =
        Sha256Digest::parse(row.try_get::<String, _>("expected_sha256")?).map_err(|_| corrupt())?;
    let mut record = UploadRecord::new(
        upload_id,
        &declared.name,
        &final_path,
        declared.max_bytes,
        rolling_sha256.clone(),
        &declared.name,
        declared.max_bytes,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(
            row.try_get::<i64, _>("received_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            UploadState::Receiving,
        )
        .map_err(|_| corrupt())?;
    if row.try_get::<String, _>("final_relative_path")? != final_path
        || row.try_get::<String, _>("staging_path")?
            != record.staging_relative_path().to_string_lossy()
    {
        return Err(corrupt());
    }
    Ok(Some(ServerLogUpload {
        upload_id,
        record,
        artifact_id,
        declaration_name: declared.name.clone(),
        rolling_sha256,
    }))
}

async fn create_log_ledger(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: SourceId,
    job_id: &str,
    task_id: &str,
    attempt_id: AttemptId,
    spec: &CompiledTaskSpec,
    now_ms: i64,
) -> Result<(), StoreError> {
    let Some(declared) = task_log_declaration(spec)? else {
        return Ok(());
    };
    let upload_id = UploadId::generate();
    let artifact_id = ArtifactId::generate();
    let (_, basename) = declaration(spec, &declared.name)?;
    let final_path = task_artifact_path(
        source_id,
        job_id.parse().map_err(|_| corrupt())?,
        task_id.parse().map_err(|_| corrupt())?,
        artifact_id,
        &basename,
    )
    .map_err(|error| StoreError::new(error.code()))?;
    let empty = sha256(&[])?;
    sqlx::query(
        "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
         final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) \
         VALUES(?,'attempt',?,NULL,?,?,?,?,?,?,0,'receiving',?,?)",
    )
    .bind(upload_id.to_string())
    .bind(attempt_id.to_string())
    .bind(&declared.name)
    .bind(artifact_id.to_string())
    .bind(format!(".staging/uploads/{upload_id}"))
    .bind(final_path)
    .bind(i64::try_from(declared.max_bytes).map_err(|_| corrupt())?)
    .bind(empty.as_str())
    .bind(now_ms)
    .bind(now_ms)
    .execute(&mut **transaction)
    .await?;
    Ok(())
}

fn task_log_declaration(
    spec: &CompiledTaskSpec,
) -> Result<Option<&flori_core::ArtifactDeclaration>, StoreError> {
    let mut logs = spec
        .artifacts
        .iter()
        .filter(|artifact| artifact.kind == ArtifactKind::TaskLog);
    let first = logs.next();
    if logs.next().is_some() {
        return Err(corrupt());
    }
    Ok(first)
}

pub(in crate::sqlite::runner) fn log_bytes(events: &[TaskLogEvent]) -> Result<Vec<u8>, StoreError> {
    let mut bytes = Vec::new();
    for (index, event) in events.iter().enumerate() {
        if event.frame.sequence != index as u64 + 1
            || serde_json::from_str::<TaskLogLine>(&event.frame.line).is_err()
        {
            return Err(corrupt());
        }
        bytes.extend_from_slice(event.frame.line.as_bytes());
        bytes.push(b'\n');
    }
    Ok(bytes)
}

pub(in crate::sqlite::runner) fn sha256(bytes: &[u8]) -> Result<Sha256Digest, StoreError> {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(value).map_err(|_| corrupt())
}

fn ensure_one(result: sqlx::sqlite::SqliteQueryResult) -> Result<(), StoreError> {
    if result.rows_affected() == 1 {
        Ok(())
    } else {
        Err(StoreError::new(ErrorCode::Conflict))
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
