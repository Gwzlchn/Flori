use flori_core::{ErrorCode, JobId};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::super::{Store, StoreError};

impl Store {
    pub async fn cancel_job(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let id = job_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query("SELECT state,source_id FROM jobs WHERE id=?")
            .bind(&id)
            .fetch_optional(&mut *transaction)
            .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::NotFound));
        };
        let source_id = row
            .try_get::<String, _>("source_id")?
            .parse()
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        match row.try_get::<String, _>("state")?.as_str() {
            "canceled" => {
                transaction.rollback().await?;
                self.reconcile_uploads(artifacts, now_ms).await?;
                self.prune_source_jobs(artifacts, source_id).await?;
                return Ok(());
            }
            "queued" | "running" => {}
            "succeeded" | "failed" => {
                transaction.rollback().await?;
                return Err(StoreError::new(ErrorCode::Conflict));
            }
            _ => return Err(StoreError::new(ErrorCode::CorruptState)),
        }
        sqlx::query(
            "UPDATE attempts SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='parent job canceled' WHERE state='leased' AND task_id IN \
             (SELECT id FROM tasks WHERE job_id=?)",
        )
        .bind(now_ms)
        .bind(&id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE tasks SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='parent job canceled' WHERE job_id=? \
             AND state IN ('pending','ready','leased')",
        )
        .bind(now_ms)
        .bind(&id)
        .execute(&mut *transaction)
        .await?;
        let changed = sqlx::query(
            "UPDATE jobs SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='job canceled' WHERE id=? AND state IN ('queued','running')",
        )
        .bind(now_ms)
        .bind(&id)
        .execute(&mut *transaction)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        self.reconcile_uploads(artifacts, now_ms).await?;
        self.prune_source_jobs(artifacts, source_id).await
    }
}
