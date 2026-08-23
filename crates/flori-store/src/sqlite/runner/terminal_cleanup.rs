use flori_core::{
    ArtifactKind, AttemptId, CompiledTaskSpec, ErrorCode, PendingAttemptUpload, RunnerId,
    Sha256Digest, UploadId,
};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, UploadRecord, task_artifact_path};

use super::{super::StoreError, upload_rule::declaration};

pub(super) async fn cleanup_failed_uploads(
    pool: &sqlx::SqlitePool,
    artifacts: &NasArtifactStore,
    runner_id: RunnerId,
    attempt_id: AttemptId,
) -> Result<(), StoreError> {
    loop {
        let mut transaction = pool.begin_with("BEGIN IMMEDIATE").await?;
        let state = sqlx::query("SELECT runner_id,state FROM attempts WHERE id=?")
            .bind(attempt_id.to_string())
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
        if state.try_get::<Option<String>, _>("runner_id")?.as_deref()
            != Some(runner_id.to_string().as_str())
            || state.try_get::<String, _>("state")? != "failed"
        {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        let row = sqlx::query(
            "SELECT u.id,u.commit_json,u.name,u.target_id,u.staging_path,u.final_relative_path, \
             u.expected_size_bytes,u.expected_sha256,u.received_bytes,u.state,t.spec_json, \
             t.id AS task_id,j.id AS job_id,j.source_id FROM uploads u JOIN attempts a \
             ON a.id=u.owner_id JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
             WHERE u.owner_kind='attempt' AND u.owner_id=? ORDER BY u.id LIMIT 1",
        )
        .bind(attempt_id.to_string())
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Ok(());
        };
        let upload_id: UploadId = row
            .try_get::<String, _>("id")?
            .parse()
            .map_err(|_| corrupt())?;
        let spec: CompiledTaskSpec =
            serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
        let record = cleanup_record(&row, upload_id, &spec)?;
        artifacts
            .discard(&record)
            .map_err(|error| StoreError::new(error.code()))?;
        let deleted =
            sqlx::query("DELETE FROM uploads WHERE id=? AND owner_kind='attempt' AND owner_id=?")
                .bind(upload_id.to_string())
                .bind(attempt_id.to_string())
                .execute(&mut *transaction)
                .await?;
        if deleted.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
    }
}

fn cleanup_record(
    row: &sqlx::sqlite::SqliteRow,
    upload_id: UploadId,
    spec: &CompiledTaskSpec,
) -> Result<UploadRecord, StoreError> {
    let name: String = row.try_get("name")?;
    let (declared, basename) = declaration(spec, &name)?;
    let commit = row.try_get::<Option<String>, _>("commit_json")?;
    let (expected_size, expected_sha, declared_name, final_path) = if let Some(json) = commit {
        let pending: PendingAttemptUpload = serde_json::from_str(&json).map_err(|_| corrupt())?;
        if pending.artifact_id.to_string() != row.try_get::<String, _>("target_id")?
            || pending.artifact.name != name
            || pending.artifact.kind != declared.kind
            || pending.declaration_name != declared.name
            || !declared
                .kind
                .accepts_media_type(&pending.artifact.media_type)
            || i64::try_from(pending.artifact.size_bytes).map_err(|_| invalid())?
                != row.try_get::<i64, _>("expected_size_bytes")?
            || pending.artifact.sha256.as_str() != row.try_get::<String, _>("expected_sha256")?
        {
            return Err(corrupt());
        }
        (
            pending.artifact.size_bytes,
            pending.artifact.sha256,
            pending.declaration_name,
            pending.artifact.relative_path,
        )
    } else {
        if declared.kind != ArtifactKind::TaskLog
            || row.try_get::<String, _>("state")? != "receiving"
            || row.try_get::<i64, _>("expected_size_bytes")?
                != i64::try_from(declared.max_bytes).map_err(|_| invalid())?
        {
            return Err(corrupt());
        }
        let final_path = task_artifact_path(
            row.try_get::<String, _>("source_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("job_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("task_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("target_id")?
                .parse()
                .map_err(|_| corrupt())?,
            &basename,
        )
        .map_err(|error| StoreError::new(error.code()))?;
        (
            row.try_get::<i64, _>("expected_size_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            Sha256Digest::parse(row.try_get::<String, _>("expected_sha256")?)
                .map_err(|_| corrupt())?,
            declared.name.clone(),
            final_path,
        )
    };
    if final_path != row.try_get::<String, _>("final_relative_path")? {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        name,
        final_path,
        expected_size,
        expected_sha,
        &declared_name,
        declared.max_bytes,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(
            row.try_get::<i64, _>("received_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            serde_json::from_str(&format!("\"{}\"", row.try_get::<String, _>("state")?))
                .map_err(|_| corrupt())?,
        )
        .map_err(|_| corrupt())?;
    if record.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok(record)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
