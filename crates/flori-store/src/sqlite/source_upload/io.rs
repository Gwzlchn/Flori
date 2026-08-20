use flori_core::{ErrorCode, Sha256Digest, UploadCursor, UploadId, UploadState};

use crate::artifact::{NasArtifactStore, RecoveryAction};
use crate::sqlite::{Store, StoreError};

impl Store {
    pub async fn append_source_upload(
        &self,
        artifacts: &NasArtifactStore,
        upload_id: UploadId,
        offset: u64,
        chunk_sha256: &Sha256Digest,
        bytes: &[u8],
        now_ms: i64,
    ) -> Result<UploadCursor, StoreError> {
        if bytes.is_empty() || now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let active = super::record::load(&mut transaction, upload_id).await?;
        let before = active.record.received_bytes();
        let received = artifacts
            .append_chunk(&active.record, offset, chunk_sha256, bytes)
            .map_err(|error| StoreError::new(error.code()))?;
        let updated = sqlx::query(
            "UPDATE uploads SET received_bytes=?,updated_at_ms=? \
             WHERE id=? AND owner_kind='source' AND state='receiving' AND received_bytes=?",
        )
        .bind(i64::try_from(received).map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?)
        .bind(now_ms)
        .bind(upload_id.to_string())
        .bind(i64::try_from(before).map_err(|_| StoreError::new(ErrorCode::CorruptState))?)
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        Ok(UploadCursor {
            upload_id,
            received_bytes: received,
        })
    }

    pub async fn verify_source_upload(
        &self,
        artifacts: &NasArtifactStore,
        upload_id: UploadId,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        {
            let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
            let active = super::record::load(&mut transaction, upload_id).await?;
            match active.record.state() {
                UploadState::Receiving => {
                    artifacts
                        .verify_staging(&active.record)
                        .map_err(|error| StoreError::new(error.code()))?;
                    let changed = sqlx::query(
                        "UPDATE uploads SET state='verified',updated_at_ms=? \
                         WHERE id=? AND owner_kind='source' AND state='receiving'",
                    )
                    .bind(now_ms)
                    .bind(upload_id.to_string())
                    .execute(&mut *transaction)
                    .await?;
                    if changed.rows_affected() != 1 {
                        return Err(StoreError::new(ErrorCode::Conflict));
                    }
                }
                UploadState::Verified | UploadState::Moved => {}
            }
            transaction.commit().await?;
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let active = super::record::load(&mut transaction, upload_id).await?;
        match active.record.state() {
            UploadState::Verified => {
                artifacts
                    .move_verified(&active.record)
                    .map_err(|error| StoreError::new(error.code()))?;
                let changed = sqlx::query(
                    "UPDATE uploads SET state='moved',updated_at_ms=? \
                     WHERE id=? AND owner_kind='source' AND state='verified'",
                )
                .bind(now_ms)
                .bind(upload_id.to_string())
                .execute(&mut *transaction)
                .await?;
                if changed.rows_affected() != 1 {
                    return Err(StoreError::new(ErrorCode::Conflict));
                }
            }
            UploadState::Moved => {
                if artifacts
                    .recovery_action(&active.record, true)
                    .map_err(|error| StoreError::new(error.code()))?
                    != RecoveryAction::RetryCommit
                {
                    return Err(StoreError::new(ErrorCode::CorruptState));
                }
            }
            UploadState::Receiving => return Err(StoreError::new(ErrorCode::CorruptState)),
        }
        transaction.commit().await?;
        Ok(())
    }
}
