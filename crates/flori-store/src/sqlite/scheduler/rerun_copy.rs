use flori_core::{ErrorCode, PendingMaterializeCommit, PendingMaterializedArtifact, UploadState};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::super::{Store, StoreError};
use super::rerun_artifact::{
    declaration, digest_bytes, parse_upload_state, source_record, source_visible, to_u64,
};

const COPY_CHUNK_BYTES: usize = 64 * 1024;

pub(super) async fn copy_all(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    request_key: &str,
    request_sha256: &str,
) -> Result<(), StoreError> {
    for artifact in &pending.artifacts {
        while !advance_one(
            store,
            artifacts,
            pending,
            artifact,
            request_key,
            request_sha256,
        )
        .await?
        {}
    }
    Ok(())
}

pub(super) async fn verify_ready(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    request_key: &str,
    request_sha256: &str,
) -> Result<(), StoreError> {
    validate_owner(transaction, pending).await?;
    for artifact in &pending.artifacts {
        let (target, source_path) =
            load_records(transaction, pending, artifact, request_key, request_sha256).await?;
        if target.state() != UploadState::Moved {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        require_exact(artifacts, &source_record(&target, &source_path)?)?;
        require_exact(artifacts, &target)?;
    }
    Ok(())
}

async fn advance_one(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
    request_key: &str,
    request_sha256: &str,
) -> Result<bool, StoreError> {
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    validate_owner(&mut transaction, pending).await?;
    let (target, source_path) = load_records(
        &mut transaction,
        pending,
        artifact,
        request_key,
        request_sha256,
    )
    .await?;
    let state = target.state();
    match state {
        UploadState::Receiving => {
            require_exact(artifacts, &source_record(&target, &source_path)?)?;
            let offset = target.received_bytes();
            let bytes = artifacts
                .read_chunk(&source_path, offset, COPY_CHUNK_BYTES)
                .map_err(|error| StoreError::new(error.code()))?;
            if bytes.is_empty() {
                if offset != target.expected_size_bytes() {
                    return Err(corrupt());
                }
                artifacts
                    .verify_staging(&target)
                    .map_err(|error| StoreError::new(error.code()))?;
                ensure_one(
                    sqlx::query(
                        "UPDATE uploads SET state='verified',updated_at_ms=? \
                         WHERE id=? AND state='receiving' AND received_bytes=?",
                    )
                    .bind(pending.created_at_ms)
                    .bind(artifact.upload_id.to_string())
                    .bind(i64::try_from(offset).map_err(|_| corrupt())?)
                    .execute(&mut *transaction)
                    .await?,
                )?;
            } else {
                let digest = digest_bytes(&bytes);
                let received = artifacts
                    .append_chunk(&target, offset, &digest, &bytes)
                    .map_err(|error| StoreError::new(error.code()))?;
                ensure_one(
                    sqlx::query(
                        "UPDATE uploads SET received_bytes=?,updated_at_ms=? \
                         WHERE id=? AND state='receiving' AND received_bytes=?",
                    )
                    .bind(i64::try_from(received).map_err(|_| corrupt())?)
                    .bind(pending.created_at_ms)
                    .bind(artifact.upload_id.to_string())
                    .bind(i64::try_from(offset).map_err(|_| corrupt())?)
                    .execute(&mut *transaction)
                    .await?,
                )?;
            }
            transaction.commit().await?;
            Ok(false)
        }
        UploadState::Verified => {
            artifacts
                .move_verified(&target)
                .map_err(|error| StoreError::new(error.code()))?;
            ensure_one(
                sqlx::query(
                    "UPDATE uploads SET state='moved',updated_at_ms=? \
                     WHERE id=? AND state='verified'",
                )
                .bind(pending.created_at_ms)
                .bind(artifact.upload_id.to_string())
                .execute(&mut *transaction)
                .await?,
            )?;
            transaction.commit().await?;
            Ok(false)
        }
        UploadState::Moved => {
            require_exact(artifacts, &target)?;
            transaction.rollback().await?;
            Ok(true)
        }
    }
}

pub(in crate::sqlite) async fn load_records(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
    request_key: &str,
    request_sha256: &str,
) -> Result<(UploadRecord, String), StoreError> {
    let row = sqlx::query(
        "SELECT u.owner_id,u.request_key,u.request_sha256,u.commit_json,u.name,u.target_id, \
         u.source_artifact_id,u.staging_path,u.final_relative_path,u.expected_size_bytes, \
         u.expected_sha256,u.received_bytes,u.state,a.source_id,a.job_id,a.attempt_id,a.origin, \
         a.materialized_from_artifact_id,a.name AS source_name,a.kind AS source_kind, \
         a.media_type AS source_media_type,a.file_name AS source_file_name, \
         a.relative_path AS source_path,a.size_bytes AS source_size,a.sha256 AS source_sha, \
         a.retention AS source_retention,t.task_key AS source_task_key,t.state AS source_task_state, \
         t.current_attempt_id AS source_current_attempt,t.id AS source_task_id, \
         x.task_id AS attempt_task_id,x.state AS attempt_state FROM uploads u \
         JOIN artifacts a ON a.id=u.source_artifact_id JOIN tasks t ON t.id=a.task_id \
         LEFT JOIN attempts x ON x.id=a.attempt_id \
         WHERE u.id=? AND u.owner_kind='materialize'",
    )
    .bind(artifact.upload_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(corrupt)?;
    let decoded: PendingMaterializeCommit =
        serde_json::from_str(row.try_get("commit_json")?).map_err(|_| corrupt())?;
    let task_key = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .map(|task| task.task_key.as_str())
        .ok_or_else(corrupt)?;
    let attempt: Option<String> = row.try_get("attempt_id")?;
    let current_attempt: Option<String> = row.try_get("source_current_attempt")?;
    let materialized_from: Option<String> = row.try_get("materialized_from_artifact_id")?;
    let visible = source_visible(
        row.try_get("origin")?,
        row.try_get("source_task_state")?,
        (
            attempt.as_deref(),
            current_attempt.as_deref(),
            materialized_from.as_deref(),
        ),
        (
            row.try_get("source_task_id")?,
            row.try_get::<Option<String>, _>("attempt_task_id")?
                .as_deref(),
            row.try_get::<Option<String>, _>("attempt_state")?
                .as_deref(),
        ),
    );
    let expected_request_key = pending
        .artifacts
        .first()
        .is_some_and(|first| artifact.upload_id == first.upload_id)
        .then_some(request_key);
    let expected_kind = serde_json::to_string(&artifact.kind).map_err(|_| corrupt())?;
    let expected_retention = serde_json::to_string(&artifact.retention).map_err(|_| corrupt())?;
    if decoded != *pending
        || row.try_get::<String, _>("owner_id")? != pending.job_id.to_string()
        || row.try_get::<Option<String>, _>("request_key")?.as_deref() != expected_request_key
        || row
            .try_get::<Option<String>, _>("request_sha256")?
            .as_deref()
            != Some(request_sha256)
        || row.try_get::<String, _>("name")? != format!("{task_key}/{}", artifact.name)
        || row.try_get::<String, _>("target_id")? != artifact.artifact_id.to_string()
        || row
            .try_get::<Option<String>, _>("source_artifact_id")?
            .as_deref()
            != Some(artifact.source_artifact_id.to_string().as_str())
        || row.try_get::<String, _>("final_relative_path")? != artifact.final_relative_path
        || to_u64(row.try_get("expected_size_bytes")?)? != artifact.size_bytes
        || row.try_get::<String, _>("expected_sha256")? != artifact.sha256.as_str()
        || !visible
        || row.try_get::<String, _>("source_id")? != pending.source_id.to_string()
        || row.try_get::<String, _>("job_id")? != pending.base_job_id.to_string()
        || row.try_get::<String, _>("source_task_key")? != task_key
        || row.try_get::<String, _>("source_name")? != artifact.name
        || row.try_get::<String, _>("source_kind")? != expected_kind.trim_matches('"')
        || row.try_get::<String, _>("source_media_type")? != artifact.media_type
        || row.try_get::<String, _>("source_file_name")? != artifact.file_name
        || to_u64(row.try_get("source_size")?)? != artifact.size_bytes
        || row.try_get::<String, _>("source_sha")? != artifact.sha256.as_str()
        || row.try_get::<String, _>("source_retention")? != expected_retention.trim_matches('"')
    {
        return Err(corrupt());
    }
    let task = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .ok_or_else(corrupt)?;
    let (declaration, _) = declaration(&task.spec.artifacts, &artifact.name)?;
    let mut target = UploadRecord::new(
        artifact.upload_id,
        &artifact.name,
        &artifact.final_relative_path,
        artifact.size_bytes,
        artifact.sha256.clone(),
        &declaration.name,
        declaration.max_bytes,
    )
    .map_err(|_| corrupt())?;
    target
        .restore_progress(
            to_u64(row.try_get("received_bytes")?)?,
            parse_upload_state(row.try_get("state")?)?,
        )
        .map_err(|_| corrupt())?;
    if target.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok((target, row.try_get("source_path")?))
}

pub(in crate::sqlite) async fn validate_owner(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    let valid: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM sources s JOIN jobs j ON j.id=s.current_job_id \
         JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id \
         WHERE s.id=? AND s.current_job_id=? AND j.state='succeeded' \
         AND j.pipeline_revision_id=?",
    )
    .bind(pending.source_id.to_string())
    .bind(pending.base_job_id.to_string())
    .bind(pending.pipeline_revision_id.to_string())
    .fetch_one(&mut **transaction)
    .await?;
    if valid != 1 {
        return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
    }
    Ok(())
}

fn require_exact(artifacts: &NasArtifactStore, record: &UploadRecord) -> Result<(), StoreError> {
    if artifacts
        .recovery_action(record, true)
        .map_err(|error| StoreError::new(error.code()))?
        != RecoveryAction::RetryCommit
    {
        return Err(corrupt());
    }
    Ok(())
}

fn ensure_one(result: sqlx::sqlite::SqliteQueryResult) -> Result<(), StoreError> {
    if result.rows_affected() != 1 {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    Ok(())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
