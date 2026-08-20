use super::{
    super::{Store, StoreError},
    wire::error_code,
};
use flori_core::{AttemptId, ErrorCode, TaskState};
use sqlx::Row;

impl Store {
    pub async fn expire_attempt(
        &self,
        attempt_id: AttemptId,
        code: ErrorCode,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        if now_ms < 0 || !matches!(code, ErrorCode::AttemptTimeout | ErrorCode::RunnerLost) {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let attempt_id = attempt_id.to_string();
        let message = match code {
            ErrorCode::AttemptTimeout => "attempt timeout",
            ErrorCode::RunnerLost => "runner lease expired",
            _ => unreachable!("validated above"),
        };
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            "SELECT a.state AS attempt_state,a.attempt_no,a.lease_expires_at_ms, \
                    t.id AS task_id,t.state AS task_state,t.attempt_limit,t.current_attempt_id, \
                    j.id AS job_id,j.state AS job_state \
             FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
             WHERE a.id=?",
        )
        .bind(&attempt_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        };
        if row.try_get::<String, _>("attempt_state")? != "leased"
            || row.try_get::<String, _>("task_state")? != "leased"
            || row.try_get::<String, _>("job_state")? != "running"
            || row
                .try_get::<Option<String>, _>("current_attempt_id")?
                .as_deref()
                != Some(attempt_id.as_str())
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        if row.try_get::<i64, _>("lease_expires_at_ms")? > now_ms {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        let task_id: String = row.try_get("task_id")?;
        let job_id: String = row.try_get("job_id")?;
        sqlx::query(
            "UPDATE attempts SET state='expired',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(error_code(code))
        .bind(message)
        .bind(&attempt_id)
        .execute(&mut *transaction)
        .await?;
        if row.try_get::<i64, _>("attempt_no")? < row.try_get::<i64, _>("attempt_limit")? {
            sqlx::query(
                "UPDATE tasks SET state='ready',current_attempt_id=NULL,ready_at_ms=?, \
                 error_code=NULL,error_message=NULL WHERE id=? AND state='leased'",
            )
            .bind(now_ms)
            .bind(task_id)
            .execute(&mut *transaction)
            .await?;
            transaction.commit().await?;
            return Ok(TaskState::Ready);
        }
        sqlx::query(
            "UPDATE tasks SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(error_code(code))
        .bind(message)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE attempts SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='parent job failed' WHERE state='leased' AND task_id IN \
             (SELECT id FROM tasks WHERE job_id=? AND id<>?)",
        )
        .bind(now_ms)
        .bind(&job_id)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE tasks SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='parent job failed' WHERE job_id=? AND id<>? \
             AND state IN ('pending','ready','leased')",
        )
        .bind(now_ms)
        .bind(&job_id)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE jobs SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='running'",
        )
        .bind(now_ms)
        .bind(error_code(code))
        .bind(message)
        .bind(job_id)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(TaskState::Failed)
    }
}
