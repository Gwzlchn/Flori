use super::super::knowledge::rebuild_source_projection;
use super::super::{Store, StoreError};
use crate::artifact::NasArtifactStore;
use flori_core::{AttemptId, ErrorCode, JobId, TaskId};
use sqlx::Row;

impl Store {
    pub async fn publish_job(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        self.publish(job_id, task_id, attempt_id, now_ms, None)
            .await
    }

    pub async fn publish_job_with_projection(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        self.publish(job_id, task_id, attempt_id, now_ms, Some(artifacts))
            .await
    }

    async fn publish(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
        artifacts: Option<&NasArtifactStore>,
    ) -> Result<(), StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let job_id = job_id.to_string();
        let task_id = task_id.to_string();
        let attempt_id = attempt_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            r#"SELECT j.state AS job_state,j.source_id AS source_id,
                    t.state AS task_state,t.executor AS executor
               FROM jobs j JOIN tasks t ON t.job_id=j.id
              WHERE j.id=? AND t.id=?"#,
        )
        .bind(&job_id)
        .bind(&task_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::NotFound));
        };
        let job_state: String = row.try_get("job_state")?;
        let task_state: String = row.try_get("task_state")?;
        let source_id: String = row.try_get("source_id")?;
        let executor: String = row.try_get("executor")?;
        if job_state == "succeeded" && task_state == "succeeded" {
            let current: Option<String> =
                sqlx::query_scalar("SELECT current_job_id FROM sources WHERE id=?")
                    .bind(&source_id)
                    .fetch_one(&mut *transaction)
                    .await?;
            transaction.rollback().await?;
            return if current.as_deref() == Some(job_id.as_str()) {
                Ok(())
            } else {
                Err(StoreError::new(ErrorCode::CorruptState))
            };
        }
        if job_state != "running" || task_state != "ready" || executor != "core.publish" {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        let unfinished: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM tasks WHERE job_id=? AND id<>? \
             AND state NOT IN ('succeeded','skipped')",
        )
        .bind(&job_id)
        .bind(&task_id)
        .fetch_one(&mut *transaction)
        .await?;
        if unfinished != 0 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }

        let has_evidence: bool = sqlx::query_scalar(
            "SELECT EXISTS(SELECT 1 FROM artifacts WHERE job_id=? AND kind='evidence')",
        )
        .bind(&job_id)
        .fetch_one(&mut *transaction)
        .await?;
        match (artifacts, has_evidence) {
            (Some(artifacts), true) => {
                rebuild_source_projection(&mut transaction, artifacts, &source_id, &job_id).await?;
            }
            (None, true) | (Some(_), false) => {
                transaction.rollback().await?;
                return Err(StoreError::new(ErrorCode::EvidenceInvalid));
            }
            (None, false) => {}
        }

        sqlx::query(
            r#"INSERT INTO attempts(
                 id,task_id,attempt_no,runner_id,state,lease_expires_at_ms,
                 last_log_sequence,started_at_ms,finished_at_ms
               ) VALUES(?,?,1,NULL,'succeeded',?,0,?,?)"#,
        )
        .bind(&attempt_id)
        .bind(&task_id)
        .bind(now_ms)
        .bind(now_ms)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE tasks SET state='succeeded',current_attempt_id=?,started_at_ms=?, \
             finished_at_ms=? WHERE id=? AND state='ready'",
        )
        .bind(&attempt_id)
        .bind(now_ms)
        .bind(now_ms)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE jobs SET state='succeeded',started_at_ms=COALESCE(started_at_ms,?), \
             finished_at_ms=? WHERE id=? AND state='running'",
        )
        .bind(now_ms)
        .bind(now_ms)
        .bind(&job_id)
        .execute(&mut *transaction)
        .await?;
        let rotated = sqlx::query(
            "UPDATE sources SET previous_job_id=current_job_id,current_job_id=?,updated_at_ms=? \
             WHERE id=?",
        )
        .bind(&job_id)
        .bind(now_ms)
        .bind(source_id)
        .execute(&mut *transaction)
        .await?;
        if rotated.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        transaction.commit().await?;
        Ok(())
    }
}
