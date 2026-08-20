use flori_core::{AttemptId, ErrorCode, JobId, TaskId};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    wire::error_code,
};

impl Store {
    pub async fn drive_core_once(
        &self,
        artifacts: &NasArtifactStore,
        now_ms: i64,
    ) -> Result<bool, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let row = sqlx::query(
            "SELECT t.id,t.job_id,t.executor FROM tasks t JOIN jobs j ON j.id=t.job_id \
             WHERE t.state='ready' AND j.state='running' \
             AND t.executor IN ('core.validate','core.publish') ORDER BY j.created_at_ms,t.task_key LIMIT 1",
        )
        .fetch_optional(&self.pool)
        .await?;
        let Some(row) = row else {
            return Ok(false);
        };
        let job_id: JobId = row
            .try_get::<String, _>("job_id")?
            .parse()
            .map_err(|_| corrupt())?;
        let task_id: TaskId = row
            .try_get::<String, _>("id")?
            .parse()
            .map_err(|_| corrupt())?;
        let attempt_id = AttemptId::generate();
        let result = match row.try_get::<String, _>("executor")?.as_str() {
            "core.validate" => self
                .validate_pdf_job(artifacts, job_id, task_id, attempt_id, now_ms)
                .await
                .map(|_| ()),
            "core.publish" => {
                self.publish_job_with_projection(artifacts, job_id, task_id, attempt_id, now_ms)
                    .await
            }
            _ => return Err(corrupt()),
        };
        match result {
            Ok(()) => Ok(true),
            Err(error) if error.code() == ErrorCode::Conflict => Ok(true),
            Err(error)
                if matches!(
                    error.code(),
                    ErrorCode::EvidenceInvalid
                        | ErrorCode::DigestMismatch
                        | ErrorCode::ArtifactInvalidPath
                        | ErrorCode::ArtifactTooLarge
                ) =>
            {
                self.fail_core_task(job_id, task_id, attempt_id, error.code(), now_ms)
                    .await?;
                Ok(true)
            }
            Err(error) => Err(error),
        }
    }

    async fn fail_core_task(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        code: ErrorCode,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        let wire = error_code(code);
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let inserted = sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,error_code,error_message,started_at_ms,finished_at_ms) \
             SELECT ?,id,1,NULL,'failed',?,0,?,?,?,? FROM tasks \
             WHERE id=? AND job_id=? AND state='ready' AND executor LIKE 'core.%'",
        )
        .bind(attempt_id.to_string())
        .bind(now_ms)
        .bind(wire)
        .bind(wire)
        .bind(now_ms)
        .bind(now_ms)
        .bind(task_id.to_string())
        .bind(job_id.to_string())
        .execute(&mut *transaction)
        .await?;
        if inserted.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        sqlx::query(
            "UPDATE tasks SET state='failed',current_attempt_id=?,error_code=?,error_message=?, \
             started_at_ms=?,finished_at_ms=? WHERE id=? AND state='ready'",
        )
        .bind(attempt_id.to_string())
        .bind(wire)
        .bind(wire)
        .bind(now_ms)
        .bind(now_ms)
        .bind(task_id.to_string())
        .execute(&mut *transaction)
        .await?;
        sqlx::query(
            "UPDATE tasks SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
             error_message='parent job failed' WHERE job_id=? AND id<>? \
             AND state IN ('pending','ready','leased')",
        )
        .bind(now_ms)
        .bind(job_id.to_string())
        .bind(task_id.to_string())
        .execute(&mut *transaction)
        .await?;
        let changed = sqlx::query(
            "UPDATE jobs SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='running'",
        )
        .bind(now_ms)
        .bind(wire)
        .bind(wire)
        .bind(job_id.to_string())
        .execute(&mut *transaction)
        .await?;
        if changed.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        Ok(())
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
