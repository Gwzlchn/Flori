use std::str::FromStr;

use flori_core::{
    ArtifactDeclaration, ArtifactKind, AttemptId, CompiledTaskSpec, ErrorCode, JobId,
    PendingAttemptUpload, TaskId, UploadId,
};
use sqlx::{Row, Sqlite, Transaction, sqlite::SqliteRow};

use crate::sqlite::{StoreError, reconcile::attempt_record::decode_record};

use super::reserve::PendingValidation;

pub(super) async fn validation_rows(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: JobId,
    task_id: TaskId,
    attempt_id: AttemptId,
) -> Result<Vec<SqliteRow>, StoreError> {
    sqlx::query(
        "SELECT u.*,t.id AS task_id,j.id AS job_id,t.spec_json,t.executor,t.state AS task_state, \
         t.current_attempt_id,a.state AS attempt_state,a.runner_id,j.source_id,j.state AS job_state, \
         (SELECT count(*) FROM artifacts x WHERE x.id=u.target_id) AS target_count FROM uploads u \
         JOIN attempts a ON a.id=u.owner_id JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
         WHERE u.owner_kind='attempt' AND u.owner_id=? AND t.id=? AND j.id=? ORDER BY u.id",
    )
    .bind(attempt_id.to_string())
    .bind(task_id.to_string())
    .bind(job_id.to_string())
    .fetch_all(&mut **transaction)
    .await
    .map_err(Into::into)
}

pub(super) fn decode(row: &SqliteRow) -> Result<PendingValidation, StoreError> {
    let owner_id: String = row.try_get("owner_id")?;
    if row.try_get::<Option<String>, _>("runner_id")?.is_some()
        || row.try_get::<String, _>("attempt_state")? != "leased"
        || row.try_get::<String, _>("task_state")? != "leased"
        || row.try_get::<String, _>("job_state")? != "running"
        || row.try_get::<String, _>("executor")? != "core.validate"
        || row
            .try_get::<Option<String>, _>("current_attempt_id")?
            .as_deref()
            != Some(owner_id.as_str())
        || row.try_get::<i64, _>("target_count")? != 0
    {
        return Err(corrupt());
    }
    let spec: CompiledTaskSpec =
        serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
    let declaration = evidence_declaration(&spec)?;
    let json = row
        .try_get::<Option<String>, _>("commit_json")?
        .ok_or_else(corrupt)?;
    let pending: PendingAttemptUpload = serde_json::from_str(&json).map_err(|_| corrupt())?;
    if pending.artifact.kind != ArtifactKind::Evidence
        || pending.declaration_name != declaration.name
        || pending.artifact.name != declaration.name
    {
        return Err(corrupt());
    }
    let upload_id = UploadId::from_str(row.try_get("id")?).map_err(|_| corrupt())?;
    Ok(PendingValidation {
        upload_id,
        pending,
        record: decode_record(row, upload_id, &spec)?,
    })
}

fn evidence_declaration(spec: &CompiledTaskSpec) -> Result<&ArtifactDeclaration, StoreError> {
    let mut values = spec.artifacts.iter().filter(|item| {
        item.kind == ArtifactKind::Evidence && item.required && item.max_files.is_none()
    });
    let value = values.next().ok_or_else(corrupt)?;
    if values.next().is_some() {
        Err(corrupt())
    } else {
        Ok(value)
    }
}

pub(super) fn stored(error: crate::artifact::ArtifactStoreError) -> StoreError {
    StoreError::new(if error.code() == ErrorCode::StorageUnavailable {
        ErrorCode::StorageUnavailable
    } else {
        ErrorCode::CorruptState
    })
}

pub(super) fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
