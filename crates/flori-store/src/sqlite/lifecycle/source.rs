use flori_core::{ErrorCode, SourceId};

use crate::artifact::NasArtifactStore;

use super::super::{Store, StoreError};

impl Store {
    pub async fn delete_source(
        &self,
        artifacts: &NasArtifactStore,
        source_id: SourceId,
    ) -> Result<(), StoreError> {
        let id = source_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM sources WHERE id=?)")
            .bind(&id)
            .fetch_one(&mut *transaction)
            .await?;
        if !exists {
            transaction.rollback().await?;
            return artifacts
                .finish_source_delete(source_id)
                .map_err(|error| StoreError::new(error.code()));
        }
        let active: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM jobs WHERE source_id=? AND state IN ('queued','running')",
        )
        .bind(&id)
        .fetch_one(&mut *transaction)
        .await?;
        let open_uploads: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM uploads u WHERE \
             (u.owner_kind='source' AND u.owner_id=?) OR \
             (u.owner_kind='attempt' AND u.owner_id IN \
               (SELECT a.id FROM attempts a JOIN tasks t ON t.id=a.task_id \
                JOIN jobs j ON j.id=t.job_id WHERE j.source_id=?)) OR \
             (u.owner_kind='materialize' AND u.source_artifact_id IN \
               (SELECT id FROM artifacts WHERE source_id=?))",
        )
        .bind(&id)
        .bind(&id)
        .bind(&id)
        .fetch_one(&mut *transaction)
        .await?;
        if active != 0 || open_uploads != 0 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::SourceBusy));
        }
        let paths: Vec<String> = sqlx::query_scalar(
            "SELECT relative_path FROM source_inputs WHERE source_id=? \
             UNION ALL SELECT relative_path FROM artifacts WHERE source_id=? ORDER BY 1",
        )
        .bind(&id)
        .bind(&id)
        .fetch_all(&mut *transaction)
        .await?;
        let staged = artifacts
            .stage_source_delete(source_id, &paths)
            .map_err(|error| StoreError::new(error.code()))?;
        let outcome = delete_rows(&mut transaction, &id).await;
        if let Err(error) = outcome {
            transaction.rollback().await?;
            if staged {
                artifacts
                    .restore_source_delete(source_id)
                    .map_err(|restore| StoreError::new(restore.code()))?;
            }
            return Err(error);
        }
        if let Err(error) = transaction.commit().await {
            self.reconcile_source_delete(artifacts, source_id).await?;
            return Err(error.into());
        }
        artifacts
            .finish_source_delete(source_id)
            .map_err(|error| StoreError::new(error.code()))
    }

    pub async fn reconcile_source_deletes(
        &self,
        artifacts: &NasArtifactStore,
    ) -> Result<(), StoreError> {
        let mut restored = false;
        for source_id in artifacts
            .source_delete_trash()
            .map_err(|error| StoreError::new(error.code()))?
        {
            restored |= self.reconcile_source_delete(artifacts, source_id).await?;
        }
        if restored {
            Err(StoreError::new(ErrorCode::Conflict))
        } else {
            Ok(())
        }
    }

    async fn reconcile_source_delete(
        &self,
        artifacts: &NasArtifactStore,
        source_id: SourceId,
    ) -> Result<bool, StoreError> {
        let exists: bool = sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM sources WHERE id=?)")
            .bind(source_id.to_string())
            .fetch_one(&self.pool)
            .await?;
        if exists {
            artifacts
                .restore_source_delete(source_id)
                .map_err(|error| StoreError::new(error.code()))?;
        } else {
            artifacts
                .finish_source_delete(source_id)
                .map_err(|error| StoreError::new(error.code()))?;
        }
        Ok(exists)
    }
}

async fn delete_rows(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    source_id: &str,
) -> Result<(), StoreError> {
    sqlx::query(
        "DELETE FROM search_chunk_evidence WHERE chunk_id IN \
         (SELECT chunk_id FROM search_chunks WHERE source_id=?)",
    )
    .bind(source_id)
    .execute(&mut **transaction)
    .await?;
    sqlx::query("DELETE FROM search_chunks WHERE source_id=?")
        .bind(source_id)
        .execute(&mut **transaction)
        .await?;
    sqlx::query(
        "DELETE FROM job_events WHERE (scope='source' AND scope_id=?) OR \
         (scope='job' AND scope_id IN (SELECT id FROM jobs WHERE source_id=?))",
    )
    .bind(source_id)
    .bind(source_id)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "UPDATE collections SET subscription_source_id=NULL,enabled=0 \
         WHERE subscription_source_id=?",
    )
    .bind(source_id)
    .execute(&mut **transaction)
    .await?;
    let deleted = sqlx::query("DELETE FROM sources WHERE id=?")
        .bind(source_id)
        .execute(&mut **transaction)
        .await?;
    if deleted.rows_affected() != 1 {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    Ok(())
}
