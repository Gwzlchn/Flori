use flori_core::{ErrorCode, PendingMaterializeCommit, PendingMaterializedArtifact, UploadState};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::super::{Store, StoreError};
use super::rerun_artifact::digest_bytes;
use super::rerun_record::{load_records, validate_owner};
use super::rerun_rewrite::rewritten_bytes;

const COPY_CHUNK_BYTES: usize = 64 * 1024;

pub(super) async fn copy_all(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    request_key: &str,
    request_sha256: &str,
) -> Result<(), StoreError> {
    for artifact in &pending.artifacts {
        let replacement = planned_bytes(
            store,
            artifacts,
            pending,
            artifact,
            request_key,
            request_sha256,
        )
        .await?;
        while !advance_one(
            store,
            artifacts,
            pending,
            artifact,
            replacement.as_deref(),
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
        let (target, source) =
            load_records(transaction, pending, artifact, request_key, request_sha256).await?;
        if target.state() != UploadState::Moved {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        require_exact(artifacts, &source)?;
        require_exact(artifacts, &target)?;
    }
    Ok(())
}

async fn advance_one(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
    replacement: Option<&[u8]>,
    request_key: &str,
    request_sha256: &str,
) -> Result<bool, StoreError> {
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    validate_owner(&mut transaction, pending).await?;
    let (target, source) = load_records(
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
            require_exact(artifacts, &source)?;
            let offset = target.received_bytes();
            let bytes = if let Some(replacement) = replacement {
                let start: usize = offset.try_into().map_err(|_| corrupt())?;
                replacement[start
                    ..replacement
                        .len()
                        .min(start.saturating_add(COPY_CHUNK_BYTES))]
                    .to_vec()
            } else {
                artifacts
                    .read_chunk(source.final_relative_path(), offset, COPY_CHUNK_BYTES)
                    .map_err(|error| StoreError::new(error.code()))?
            };
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

async fn planned_bytes(
    store: &Store,
    artifacts: &NasArtifactStore,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
    request_key: &str,
    request_sha256: &str,
) -> Result<Option<Vec<u8>>, StoreError> {
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    validate_owner(&mut transaction, pending).await?;
    let (_, source) = load_records(
        &mut transaction,
        pending,
        artifact,
        request_key,
        request_sha256,
    )
    .await?;
    transaction.rollback().await?;
    rewritten_bytes(artifacts, &pending.artifacts, artifact, &source)
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
