use flori_core::{
    ErrorCode, RunnerId, Sha256Digest, UploadCursor, UploadId, UploadState, VerifyUploadRequest,
    VerifyUploadResponse,
};

use crate::artifact::{NasArtifactStore, RecoveryAction};

use super::{
    super::{Store, StoreError},
    upload::load_upload,
};

impl Store {
    #[allow(clippy::too_many_arguments)]
    pub async fn append_attempt_upload(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        upload_id: UploadId,
        offset: u64,
        chunk_sha256: &Sha256Digest,
        bytes: &[u8],
        now_ms: i64,
    ) -> Result<UploadCursor, StoreError> {
        if now_ms < 0 || bytes.is_empty() {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let loaded = load_upload(&mut transaction, runner_id, upload_id, now_ms).await?;
        let received = artifacts
            .append_chunk(&loaded.record, offset, chunk_sha256, bytes)
            .map_err(|error| StoreError::new(error.code()))?;
        let updated = sqlx::query(
            "UPDATE uploads SET received_bytes=?,updated_at_ms=? \
             WHERE id=? AND state='receiving' AND received_bytes<=?",
        )
        .bind(i64::try_from(received).map_err(|_| invalid())?)
        .bind(now_ms)
        .bind(upload_id.to_string())
        .bind(i64::try_from(received).map_err(|_| invalid())?)
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

    pub async fn verify_attempt_upload(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        upload_id: UploadId,
        request: &VerifyUploadRequest,
        now_ms: i64,
    ) -> Result<VerifyUploadResponse, StoreError> {
        if now_ms < 0 {
            return Err(invalid());
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let mut loaded = load_upload(&mut transaction, runner_id, upload_id, now_ms).await?;
        if loaded.record.expected_size_bytes() != request.size_bytes
            || loaded.record.expected_sha256() != &request.sha256
        {
            return Err(StoreError::new(ErrorCode::DigestMismatch));
        }
        if loaded.record.state() == UploadState::Receiving {
            artifacts
                .verify_staging(&loaded.record)
                .map_err(|error| StoreError::new(error.code()))?;
            sqlx::query("UPDATE uploads SET state='verified',updated_at_ms=? WHERE id=? AND state='receiving'")
                .bind(now_ms).bind(upload_id.to_string()).execute(&mut *transaction).await?;
            loaded
                .record
                .restore_progress(request.size_bytes, UploadState::Verified)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        }
        if loaded.record.state() == UploadState::Verified {
            artifacts
                .move_verified(&loaded.record)
                .map_err(|error| StoreError::new(error.code()))?;
            sqlx::query(
                "UPDATE uploads SET state='moved',updated_at_ms=? WHERE id=? AND state='verified'",
            )
            .bind(now_ms)
            .bind(upload_id.to_string())
            .execute(&mut *transaction)
            .await?;
        } else if loaded.record.state() == UploadState::Moved
            && artifacts
                .recovery_action(&loaded.record, true)
                .map_err(|error| StoreError::new(error.code()))?
                != RecoveryAction::RetryCommit
        {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        transaction.commit().await?;
        Ok(VerifyUploadResponse {
            upload_id,
            artifact: loaded.pending.artifact,
        })
    }
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
