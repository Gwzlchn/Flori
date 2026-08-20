use std::fmt::Write;

use flori_core::{
    ArtifactDeclaration, ErrorCode, PendingMaterializeCommit, PendingMaterializedArtifact,
    Sha256Digest, UploadId, UploadState,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::super::{Store, StoreError};

const COPY_CHUNK_BYTES: usize = 64 * 1024;

pub(super) async fn copy_all(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    for artifact in &pending.artifacts {
        while !advance_one(store, artifacts, pending, artifact).await? {}
    }
    Ok(())
}

pub(super) async fn verify_ready(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    validate_owner(transaction, pending).await?;
    for artifact in &pending.artifacts {
        let (target, source_path) = load_records(transaction, pending, artifact).await?;
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
) -> Result<bool, StoreError> {
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    validate_owner(&mut transaction, pending).await?;
    let (target, source_path) = load_records(&mut transaction, pending, artifact).await?;
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

async fn load_records(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
) -> Result<(UploadRecord, String), StoreError> {
    let row = sqlx::query(
        "SELECT u.owner_id,u.commit_json,u.name,u.target_id,u.source_artifact_id,u.staging_path, \
         u.final_relative_path,u.expected_size_bytes,u.expected_sha256,u.received_bytes,u.state, \
         a.relative_path AS source_path,a.size_bytes AS source_size,a.sha256 AS source_sha \
         FROM uploads u JOIN artifacts a ON a.id=u.source_artifact_id \
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
    if decoded != *pending
        || row.try_get::<String, _>("owner_id")? != pending.job_id.to_string()
        || row.try_get::<String, _>("name")? != format!("{task_key}/{}", artifact.name)
        || row.try_get::<String, _>("target_id")? != artifact.artifact_id.to_string()
        || row
            .try_get::<Option<String>, _>("source_artifact_id")?
            .as_deref()
            != Some(artifact.source_artifact_id.to_string().as_str())
        || row.try_get::<String, _>("final_relative_path")? != artifact.final_relative_path
        || to_u64(row.try_get("expected_size_bytes")?)? != artifact.size_bytes
        || row.try_get::<String, _>("expected_sha256")? != artifact.sha256.as_str()
        || to_u64(row.try_get("source_size")?)? != artifact.size_bytes
        || row.try_get::<String, _>("source_sha")? != artifact.sha256.as_str()
    {
        return Err(corrupt());
    }
    let declaration = declaration(pending, artifact)?;
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
            parse_state(row.try_get("state")?)?,
        )
        .map_err(|_| corrupt())?;
    if target.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok((target, row.try_get("source_path")?))
}

fn source_record(target: &UploadRecord, source_path: &str) -> Result<UploadRecord, StoreError> {
    let mut source = UploadRecord::new(
        UploadId::generate(),
        "source",
        source_path,
        target.expected_size_bytes(),
        target.expected_sha256().clone(),
        "source",
        target.expected_size_bytes(),
    )
    .map_err(|_| corrupt())?;
    source
        .restore_progress(target.expected_size_bytes(), UploadState::Moved)
        .map_err(|_| corrupt())?;
    Ok(source)
}

fn declaration<'a>(
    pending: &'a PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
) -> Result<&'a ArtifactDeclaration, StoreError> {
    let task = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .ok_or_else(corrupt)?;
    task.spec
        .artifacts
        .iter()
        .find(|item| {
            artifact.name == item.name
                || item.max_files.is_some() && artifact.name.starts_with(&format!("{}/", item.name))
        })
        .ok_or_else(corrupt)
}

async fn validate_owner(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    let valid: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM sources s JOIN jobs j ON j.id=s.current_job_id \
         JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id \
         JOIN pipelines p ON p.id=r.pipeline_id WHERE s.id=? AND s.current_job_id=? \
         AND j.state='succeeded' AND j.pipeline_revision_id=? AND p.current_revision_id=?",
    )
    .bind(pending.source_id.to_string())
    .bind(pending.base_job_id.to_string())
    .bind(pending.pipeline_revision_id.to_string())
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

fn digest_bytes(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(value).expect("SHA-256 formatter is canonical")
}

fn parse_state(value: &str) -> Result<UploadState, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn to_u64(value: i64) -> Result<u64, StoreError> {
    value.try_into().map_err(|_| corrupt())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
