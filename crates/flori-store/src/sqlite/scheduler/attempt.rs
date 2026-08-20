use super::{
    super::{Store, StoreError},
    wire::{error_code, transient},
};
use flori_core::{AttemptId, ErrorCode, TaskState};
use sqlx::Row;

impl Store {
    pub async fn complete_attempt(
        &self,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let attempt_id = attempt_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            r#"SELECT a.state AS 'attempt_state!',a.lease_expires_at_ms AS 'lease_expires_at_ms!',
                    t.id AS 'task_id!',t.state AS 'task_state!',
                    t.current_attempt_id AS 'current_attempt_id?',j.id AS 'job_id!',
                    j.state AS 'job_state!',
                    (SELECT count(*) FROM ai_usage u
                     WHERE u.attempt_id=a.id AND u.state='started') AS 'open_usage!'
               FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id
              WHERE a.id=?"#,
        )
        .bind(&attempt_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        };
        let attempt_state: String = row.try_get("attempt_state!")?;
        let task_state: String = row.try_get("task_state!")?;
        let job_state: String = row.try_get("job_state!")?;
        let current_attempt_id: Option<String> = row.try_get("current_attempt_id?")?;
        if attempt_state == "succeeded" && task_state == "succeeded" {
            transaction.rollback().await?;
            return Ok(TaskState::Succeeded);
        }
        if attempt_state != "leased"
            || task_state != "leased"
            || job_state != "running"
            || current_attempt_id.as_deref() != Some(attempt_id.as_str())
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        if row.try_get::<i64, _>("lease_expires_at_ms!")? <= now_ms {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::LeaseExpired));
        }
        if row.try_get::<i64, _>("open_usage!")? != 0 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }

        sqlx::query(
            "UPDATE attempts SET state='succeeded',finished_at_ms=? WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(&attempt_id)
        .execute(&mut *transaction)
        .await?;
        let task_id: String = row.try_get("task_id!")?;
        let job_id: String = row.try_get("job_id!")?;
        sqlx::query(
            "UPDATE tasks SET state='succeeded',finished_at_ms=? WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(task_id)
        .execute(&mut *transaction)
        .await?;
        promote_ready(&mut transaction, &job_id, now_ms).await?;
        transaction.commit().await?;
        Ok(TaskState::Succeeded)
    }

    pub async fn fail_attempt(
        &self,
        attempt_id: AttemptId,
        code: ErrorCode,
        message: &str,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        if now_ms < 0 || message.is_empty() {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let attempt_id = attempt_id.to_string();
        let error_code = error_code(code);
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            r#"SELECT a.state AS 'attempt_state!',a.attempt_no AS 'attempt_no!',
                    a.lease_expires_at_ms AS 'lease_expires_at_ms!',t.id AS 'task_id!',
                    t.state AS 'task_state!',t.attempt_limit AS 'attempt_limit!',
                    t.current_attempt_id AS 'current_attempt_id?',j.id AS 'job_id!',
                    j.state AS 'job_state!'
               FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id
              WHERE a.id=?"#,
        )
        .bind(&attempt_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        };
        let attempt_state: String = row.try_get("attempt_state!")?;
        let task_state: String = row.try_get("task_state!")?;
        let job_state: String = row.try_get("job_state!")?;
        let current_attempt_id: Option<String> = row.try_get("current_attempt_id?")?;
        if attempt_state != "leased"
            || task_state != "leased"
            || job_state != "running"
            || current_attempt_id.as_deref() != Some(attempt_id.as_str())
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        if row.try_get::<i64, _>("lease_expires_at_ms!")? <= now_ms {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::LeaseExpired));
        }
        sqlx::query(
            "UPDATE attempts SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(error_code)
        .bind(message)
        .bind(&attempt_id)
        .execute(&mut *transaction)
        .await?;

        let task_id: String = row.try_get("task_id!")?;
        let job_id: String = row.try_get("job_id!")?;
        let attempt_no: i64 = row.try_get("attempt_no!")?;
        let attempt_limit: i64 = row.try_get("attempt_limit!")?;
        if transient(code) && attempt_no < attempt_limit {
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
        .bind(error_code)
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
        .bind(error_code)
        .bind(message)
        .bind(job_id)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(TaskState::Failed)
    }
}

pub(super) async fn promote_ready(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    job_id: &str,
    now_ms: i64,
) -> Result<(), StoreError> {
    sqlx::query(
        r#"UPDATE tasks AS candidate SET state='ready',ready_at_ms=?
             WHERE candidate.job_id=? AND candidate.state='pending'
               AND NOT EXISTS(
                 SELECT 1 FROM json_each(candidate.spec_json,'$.needs') dependency
                 LEFT JOIN tasks predecessor
                   ON predecessor.job_id=candidate.job_id
                  AND predecessor.task_key=dependency.value
                 WHERE predecessor.id IS NULL
                    OR predecessor.state NOT IN ('succeeded','skipped')
               )"#,
    )
    .bind(now_ms)
    .bind(job_id)
    .execute(&mut **transaction)
    .await?;
    Ok(())
}
