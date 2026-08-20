use flori_core::{ErrorCode, SourceId, UploadId, UploadState};

use crate::artifact::{NasArtifactStore, RecoveryAction};
use crate::sqlite::{Store, StoreError, scheduler::source_kind};

impl Store {
    pub async fn commit_source_upload(
        &self,
        artifacts: &NasArtifactStore,
        upload_id: UploadId,
        now_ms: i64,
    ) -> Result<SourceId, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let active = super::record::load(&mut transaction, upload_id).await?;
        if active.record.state() != UploadState::Moved
            || artifacts
                .recovery_action(&active.record, true)
                .map_err(|error| StoreError::new(error.code()))?
                != RecoveryAction::RetryCommit
        {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        let pending = active.pending;
        if pending.created_at_ms > now_ms
            || active.request_key.is_empty()
            || active.request_sha256.len() != 64
        {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        let domain = sqlx::query_scalar::<_, i64>("SELECT count(*) FROM domains WHERE id=?")
            .bind(pending.domain_id.to_string())
            .fetch_one(&mut *transaction)
            .await?;
        if domain != 1 {
            return Err(StoreError::new(ErrorCode::NotFound));
        }
        for collection_id in &pending.collection_ids {
            let count = sqlx::query_scalar::<_, i64>(
                "SELECT count(*) FROM collections WHERE id=? AND domain_id=?",
            )
            .bind(collection_id.to_string())
            .bind(pending.domain_id.to_string())
            .fetch_one(&mut *transaction)
            .await?;
            if count != 1 {
                return Err(StoreError::new(ErrorCode::NotFound));
            }
        }
        sqlx::query(
            "INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key,request_sha256, \
             created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        )
        .bind(pending.source_id.to_string())
        .bind(source_kind(pending.kind))
        .bind(&pending.canonical_ref)
        .bind(&pending.title)
        .bind(pending.domain_id.to_string())
        .bind(&active.request_key)
        .bind(&active.request_sha256)
        .bind(pending.created_at_ms)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await
        .map_err(|error| match &error {
            sqlx::Error::Database(database) if database.is_unique_violation() => {
                StoreError::new(ErrorCode::Conflict)
            }
            _ => error.into(),
        })?;
        sqlx::query(
            "INSERT INTO source_inputs(id,source_id,name,media_type,size_bytes,sha256,relative_path, \
             created_at_ms) VALUES(?,?,'original',?,?,?,?,?)",
        )
        .bind(pending.source_input_id.to_string())
        .bind(pending.source_id.to_string())
        .bind(&pending.media_type)
        .bind(i64::try_from(pending.size_bytes).map_err(|_| StoreError::new(ErrorCode::CorruptState))?)
        .bind(pending.sha256.as_str())
        .bind(&pending.final_relative_path)
        .bind(pending.created_at_ms)
        .execute(&mut *transaction)
        .await?;
        for collection_id in &pending.collection_ids {
            sqlx::query(
                "INSERT INTO collection_sources(collection_id,source_id,added_at_ms) VALUES(?,?,?)",
            )
            .bind(collection_id.to_string())
            .bind(pending.source_id.to_string())
            .bind(now_ms)
            .execute(&mut *transaction)
            .await?;
        }
        let deleted = sqlx::query("DELETE FROM uploads WHERE id=? AND owner_kind='source'")
            .bind(upload_id.to_string())
            .execute(&mut *transaction)
            .await?;
        if deleted.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        transaction.commit().await?;
        Ok(pending.source_id)
    }
}
