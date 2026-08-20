use flori_core::{ErrorCode, SourceId, UploadId};
use sqlx::Row;

use crate::{
    artifact::{NasArtifactStore, RecoveryAction},
    sqlite::{Store, StoreError},
};

pub(super) async fn reconcile(
    store: &Store,
    artifacts: &NasArtifactStore,
    owner_id: &str,
    now_ms: i64,
) -> Result<(), StoreError> {
    let source_id: SourceId = owner_id.parse().map_err(|_| corrupt())?;
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    let row =
        sqlx::query("SELECT id FROM uploads WHERE owner_kind='source' AND owner_id=? ORDER BY id")
            .bind(owner_id)
            .fetch_all(&mut *transaction)
            .await?;
    if row.len() != 1 {
        return Err(corrupt());
    }
    let upload_id: UploadId = row[0]
        .try_get::<String, _>("id")?
        .parse()
        .map_err(|_| corrupt())?;
    let active = super::super::source_upload::record::load(&mut transaction, upload_id).await?;
    if active.pending.source_id != source_id {
        return Err(corrupt());
    }
    let action = artifacts
        .recovery_action(&active.record, true)
        .map_err(|error| StoreError::new(error.code()))?;
    transaction.rollback().await?;
    match action {
        RecoveryAction::ResumeReceiving => Ok(()),
        RecoveryAction::MoveVerified | RecoveryAction::MarkMoved => {
            store
                .verify_source_upload(artifacts, upload_id, now_ms)
                .await?;
            store
                .commit_source_upload(artifacts, upload_id, now_ms)
                .await
                .map(|_| ())
        }
        RecoveryAction::RetryCommit => store
            .commit_source_upload(artifacts, upload_id, now_ms)
            .await
            .map(|_| ()),
        RecoveryAction::DeleteFilesThenLedger | RecoveryAction::DeleteLedger => Err(corrupt()),
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
