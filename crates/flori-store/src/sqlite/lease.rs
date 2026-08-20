use super::{Lease, Store, StoreError};
use flori_core::{AttemptId, ErrorCode, RunnerId, TaskId};

impl Store {
    pub async fn lease_task(
        &self,
        task_id: TaskId,
        attempt_id: AttemptId,
        runner_id: RunnerId,
        now_ms: i64,
        lease_expires_at_ms: i64,
    ) -> Result<Lease, StoreError> {
        if now_ms < 0 || lease_expires_at_ms <= now_ms {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }

        let task_id = task_id.to_string();
        let attempt_id_text = attempt_id.to_string();
        let runner_id = runner_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let started_job = sqlx::query!(
            r#"UPDATE jobs SET state='running',started_at_ms=COALESCE(started_at_ms,?)
               WHERE id=(SELECT job_id FROM tasks WHERE id=?)
                 AND state IN ('queued','running')"#,
            now_ms,
            task_id,
        )
        .execute(&mut *transaction)
        .await?;
        if started_job.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        let inserted = sqlx::query!(
            r#"INSERT INTO attempts(
                 id,task_id,attempt_no,runner_id,state,model,effort,runner_config_revision,
                 lease_expires_at_ms,last_log_sequence,started_at_ms
             ) SELECT ?,t.id,
                 COALESCE((SELECT MAX(previous.attempt_no)+1 FROM attempts previous WHERE previous.task_id=t.id),1),
                 ?,'leased',t.selected_model,t.selected_effort,t.runner_config_revision,?,0,?
             FROM tasks t JOIN jobs j ON j.id=t.job_id JOIN runners r ON r.id=?
             WHERE t.id=? AND t.state='ready' AND j.state='running' AND r.state='enabled'
               AND (t.ready_at_ms IS NULL OR t.ready_at_ms<=?)
               AND (t.pinned_runner_id IS NULL OR t.pinned_runner_id=r.id)
               AND (SELECT COUNT(*) FROM attempts previous WHERE previous.task_id=t.id)<t.attempt_limit"#,
            attempt_id_text,
            runner_id,
            lease_expires_at_ms,
            now_ms,
            runner_id,
            task_id,
            now_ms,
        )
        .execute(&mut *transaction)
        .await?;
        if inserted.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }

        let updated = sqlx::query!(
            r#"UPDATE tasks SET state='leased',current_attempt_id=?,
                 started_at_ms=COALESCE(started_at_ms,?)
             WHERE id=? AND state='ready'
               AND EXISTS(SELECT 1 FROM jobs WHERE id=tasks.job_id AND state='running')"#,
            attempt_id_text,
            now_ms,
            task_id,
        )
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        Ok(Lease {
            attempt_id,
            lease_expires_at_ms,
        })
    }

    pub async fn renew_lease(
        &self,
        attempt_id: AttemptId,
        runner_id: RunnerId,
        now_ms: i64,
        lease_expires_at_ms: i64,
    ) -> Result<Lease, StoreError> {
        if now_ms < 0 || lease_expires_at_ms <= now_ms {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }

        let attempt_id_text = attempt_id.to_string();
        let runner_id = runner_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let updated = sqlx::query!(
            r#"UPDATE attempts SET lease_expires_at_ms=?
             WHERE id=? AND runner_id=? AND state='leased'
               AND lease_expires_at_ms>? AND lease_expires_at_ms<=?
               AND EXISTS(
                 SELECT 1 FROM tasks t JOIN jobs j ON j.id=t.job_id
                 WHERE t.id=attempts.task_id AND t.state='leased'
                   AND t.current_attempt_id=attempts.id AND j.state='running'
               )"#,
            lease_expires_at_ms,
            attempt_id_text,
            runner_id,
            now_ms,
            lease_expires_at_ms,
        )
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() == 1 {
            transaction.commit().await?;
            return Ok(Lease {
                attempt_id,
                lease_expires_at_ms,
            });
        }

        let row = sqlx::query!(
            r#"SELECT a.runner_id AS 'runner_id?',a.state AS 'state!',
                    a.lease_expires_at_ms AS 'lease_expires_at_ms!',
                    t.state AS 'task_state!',t.current_attempt_id AS 'current_attempt_id?',
                    j.state AS 'job_state!'
             FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id
             WHERE a.id=?"#,
            attempt_id_text,
        )
        .fetch_optional(&mut *transaction)
        .await?;
        transaction.rollback().await?;
        let Some(row) = row else {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        };
        if row.state != "leased"
            || row.task_state != "leased"
            || row.job_state != "running"
            || row.current_attempt_id.as_deref() != Some(attempt_id_text.as_str())
            || row.runner_id.as_deref() != Some(runner_id.as_str())
        {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        if row.lease_expires_at_ms <= now_ms {
            Err(StoreError::new(ErrorCode::LeaseExpired))
        } else {
            Err(StoreError::new(ErrorCode::Conflict))
        }
    }
}
