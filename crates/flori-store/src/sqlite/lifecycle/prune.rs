use flori_core::{ErrorCode, JobId, SourceId};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::super::{Store, StoreError};

impl Store {
    pub async fn prune_source_jobs(
        &self,
        artifacts: &NasArtifactStore,
        source_id: SourceId,
    ) -> Result<(), StoreError> {
        let rows = sqlx::query(
            "SELECT j.id,j.state,j.id=(SELECT id FROM jobs WHERE source_id=? AND state='failed' \
             ORDER BY finished_at_ms DESC,id DESC LIMIT 1) AS keep_audit FROM jobs j \
             JOIN sources s ON s.id=j.source_id WHERE j.source_id=? \
             AND j.state IN ('succeeded','failed','canceled') \
             AND j.id NOT IN (COALESCE(s.current_job_id,''),COALESCE(s.previous_job_id,'')) \
             ORDER BY j.created_at_ms,j.id",
        )
        .bind(source_id.to_string())
        .bind(source_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        for row in rows {
            let job_id = row
                .try_get::<String, _>("id")?
                .parse()
                .map_err(|_| corrupt())?;
            self.prune_one_job(artifacts, source_id, job_id, row.try_get("keep_audit")?)
                .await?;
        }
        Ok(())
    }

    pub async fn prune_all_job_artifacts(
        &self,
        artifacts: &NasArtifactStore,
    ) -> Result<(), StoreError> {
        let sources: Vec<String> = sqlx::query_scalar("SELECT id FROM sources ORDER BY id")
            .fetch_all(&self.pool)
            .await?;
        for source in sources {
            self.prune_source_jobs(artifacts, source.parse().map_err(|_| corrupt())?)
                .await?;
        }
        Ok(())
    }

    pub async fn reconcile_job_prunes(
        &self,
        artifacts: &NasArtifactStore,
    ) -> Result<(), StoreError> {
        for job_id in artifacts
            .job_prune_trash()
            .map_err(|error| StoreError::new(error.code()))?
        {
            self.reconcile_job_prune(artifacts, job_id).await?;
        }
        Ok(())
    }

    async fn prune_one_job(
        &self,
        artifacts: &NasArtifactStore,
        source_id: SourceId,
        job_id: JobId,
        keep_audit: bool,
    ) -> Result<(), StoreError> {
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let eligible: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM jobs j JOIN sources s ON s.id=j.source_id \
             WHERE j.id=? AND j.source_id=? AND j.state IN ('succeeded','failed','canceled') \
             AND j.id NOT IN (COALESCE(s.current_job_id,''),COALESCE(s.previous_job_id,'')))",
        )
        .bind(job_id.to_string())
        .bind(source_id.to_string())
        .fetch_one(&mut *transaction)
        .await?;
        let latest_failed: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM jobs WHERE id=? AND state='failed' AND id= \
             (SELECT id FROM jobs WHERE source_id=? AND state='failed' \
              ORDER BY finished_at_ms DESC,id DESC LIMIT 1))",
        )
        .bind(job_id.to_string())
        .bind(source_id.to_string())
        .fetch_one(&mut *transaction)
        .await?;
        if !eligible || latest_failed != keep_audit {
            transaction.rollback().await?;
            return Ok(());
        }
        let referenced: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM uploads u JOIN artifacts a ON a.id=u.source_artifact_id \
             WHERE u.owner_kind='materialize' AND a.job_id=?)",
        )
        .bind(job_id.to_string())
        .fetch_one(&mut *transaction)
        .await?;
        if referenced {
            transaction.rollback().await?;
            return Ok(());
        }
        let prefix = format!("sources/{source_id}/jobs/{job_id}/");
        let rows = sqlx::query(
            "SELECT id,kind,retention,relative_path FROM artifacts WHERE job_id=? ORDER BY id",
        )
        .bind(job_id.to_string())
        .fetch_all(&mut *transaction)
        .await?;
        let mut all_paths = Vec::new();
        let mut retained_paths = Vec::new();
        let mut delete_ids = Vec::new();
        for row in rows {
            let path: String = row.try_get("relative_path")?;
            let retention: String = row.try_get("retention")?;
            if !path.starts_with(&prefix) {
                if retention != "source" {
                    return Err(corrupt());
                }
                continue;
            }
            if retention == "source" {
                return Err(corrupt());
            }
            all_paths.push(path.clone());
            if keep_audit
                && matches!(
                    row.try_get::<String, _>("kind")?.as_str(),
                    "task_log" | "ai_audit"
                )
            {
                retained_paths.push(path);
            } else {
                delete_ids.push(row.try_get::<String, _>("id")?);
            }
        }
        let staged = artifacts
            .stage_job_prune(source_id, job_id, &all_paths, &retained_paths)
            .map_err(|error| StoreError::new(error.code()))?;
        for id in delete_ids {
            if let Err(error) = sqlx::query("DELETE FROM artifacts WHERE id=?")
                .bind(id)
                .execute(&mut *transaction)
                .await
            {
                transaction.rollback().await?;
                if staged {
                    artifacts
                        .reconcile_job_prune(source_id, job_id, &all_paths)
                        .map_err(|restore| StoreError::new(restore.code()))?;
                }
                return Err(error.into());
            }
        }
        if let Err(error) = transaction.commit().await {
            self.reconcile_job_prune(artifacts, job_id).await?;
            return Err(error.into());
        }
        artifacts
            .finish_job_prune(job_id)
            .map_err(|error| StoreError::new(error.code()))
    }

    async fn reconcile_job_prune(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
    ) -> Result<(), StoreError> {
        let source: Option<String> = sqlx::query_scalar("SELECT source_id FROM jobs WHERE id=?")
            .bind(job_id.to_string())
            .fetch_optional(&self.pool)
            .await?;
        let Some(source) = source else {
            return artifacts
                .finish_job_prune(job_id)
                .map_err(|error| StoreError::new(error.code()));
        };
        let source_id = source.parse().map_err(|_| corrupt())?;
        let prefix = format!("sources/{source_id}/jobs/{job_id}/%");
        let paths: Vec<String> = sqlx::query_scalar(
            "SELECT relative_path FROM artifacts WHERE job_id=? AND relative_path LIKE ? ORDER BY 1",
        )
        .bind(job_id.to_string())
        .bind(prefix)
        .fetch_all(&self.pool)
        .await?;
        artifacts
            .reconcile_job_prune(source_id, job_id, &paths)
            .map_err(|error| StoreError::new(error.code()))
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
