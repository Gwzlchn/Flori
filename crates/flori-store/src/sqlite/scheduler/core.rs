use super::{
    super::{Store, StoreError},
    attempt::promote_ready,
};
use flori_core::{AttemptId, ErrorCode, JobId, TaskId, TaskState};
use sqlx::Row;

impl Store {
    pub async fn complete_core_task(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let job_id = job_id.to_string();
        let task_id = task_id.to_string();
        let attempt_id = attempt_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            "SELECT j.state AS job_state,t.state AS task_state,t.executor AS executor \
             FROM jobs j JOIN tasks t ON t.job_id=j.id WHERE j.id=? AND t.id=?",
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
        let executor: String = row.try_get("executor")?;
        if task_state == "succeeded" && matches!(job_state.as_str(), "running" | "succeeded") {
            transaction.rollback().await?;
            return Ok(TaskState::Succeeded);
        }
        if task_state != "ready"
            || !matches!(job_state.as_str(), "queued" | "running")
            || executor != "core.validate"
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        sqlx::query(
            "UPDATE jobs SET state='running',started_at_ms=COALESCE(started_at_ms,?) \
             WHERE id=? AND state IN ('queued','running')",
        )
        .bind(now_ms)
        .bind(&job_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms,finished_at_ms) \
             VALUES(?,?,1,NULL,'succeeded',?,0,?,?)",
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
        .bind(attempt_id)
        .bind(now_ms)
        .bind(now_ms)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await?;
        promote_ready(&mut transaction, &job_id, now_ms).await?;
        transaction.commit().await?;
        Ok(TaskState::Succeeded)
    }
}
