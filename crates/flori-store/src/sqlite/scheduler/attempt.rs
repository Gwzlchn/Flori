use super::{
    super::StoreError,
    wire::{error_code, transient},
};
use flori_core::{CompiledTaskSpec, ErrorCode, TaskState};
use sqlx::{Row, Sqlite, Transaction};

fn ensure_one(result: sqlx::sqlite::SqliteQueryResult) -> Result<(), StoreError> {
    if result.rows_affected() != 1 {
        return Err(StoreError::new(ErrorCode::StaleAttempt));
    }
    Ok(())
}

pub(crate) async fn finish_success(
    transaction: &mut Transaction<'_, Sqlite>,
    attempt_id: &str,
    task_id: &str,
    job_id: &str,
    now_ms: i64,
) -> Result<TaskState, StoreError> {
    ensure_one(
        sqlx::query(
            "UPDATE attempts SET state='succeeded',finished_at_ms=? \
             WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(attempt_id)
        .execute(&mut **transaction)
        .await?,
    )?;
    ensure_one(
        sqlx::query(
            "UPDATE tasks SET state='succeeded',finished_at_ms=? \
             WHERE id=? AND state='leased' AND current_attempt_id=?",
        )
        .bind(now_ms)
        .bind(task_id)
        .bind(attempt_id)
        .execute(&mut **transaction)
        .await?,
    )?;
    promote_ready(transaction, job_id, now_ms).await?;
    Ok(TaskState::Succeeded)
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn finish_failure(
    transaction: &mut Transaction<'_, Sqlite>,
    attempt_id: &str,
    task_id: &str,
    job_id: &str,
    attempt_no: i64,
    attempt_limit: i64,
    code: ErrorCode,
    now_ms: i64,
) -> Result<TaskState, StoreError> {
    let wire_code = error_code(code);
    ensure_one(
        sqlx::query(
            "UPDATE attempts SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='leased'",
        )
        .bind(now_ms)
        .bind(wire_code)
        .bind(wire_code)
        .bind(attempt_id)
        .execute(&mut **transaction)
        .await?,
    )?;
    if transient(code) && attempt_no < attempt_limit {
        ensure_one(
            sqlx::query(
                "UPDATE tasks SET state='ready',current_attempt_id=NULL,ready_at_ms=?, \
                 error_code=NULL,error_message=NULL WHERE id=? AND state='leased' \
                 AND current_attempt_id=?",
            )
            .bind(now_ms)
            .bind(task_id)
            .bind(attempt_id)
            .execute(&mut **transaction)
            .await?,
        )?;
        return Ok(TaskState::Ready);
    }
    ensure_one(
        sqlx::query(
            "UPDATE tasks SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='leased' AND current_attempt_id=?",
        )
        .bind(now_ms)
        .bind(wire_code)
        .bind(wire_code)
        .bind(task_id)
        .bind(attempt_id)
        .execute(&mut **transaction)
        .await?,
    )?;
    sqlx::query(
        "UPDATE attempts SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
         error_message='parent job failed' WHERE state='leased' AND task_id IN \
         (SELECT id FROM tasks WHERE job_id=? AND id<>?)",
    )
    .bind(now_ms)
    .bind(job_id)
    .bind(task_id)
    .execute(&mut **transaction)
    .await?;
    sqlx::query(
        "UPDATE tasks SET state='canceled',finished_at_ms=?,error_code='task_canceled', \
         error_message='parent job failed' WHERE job_id=? AND id<>? \
         AND state IN ('pending','ready','leased')",
    )
    .bind(now_ms)
    .bind(job_id)
    .bind(task_id)
    .execute(&mut **transaction)
    .await?;
    ensure_one(
        sqlx::query(
            "UPDATE jobs SET state='failed',finished_at_ms=?,error_code=?,error_message=? \
             WHERE id=? AND state='running'",
        )
        .bind(now_ms)
        .bind(wire_code)
        .bind(wire_code)
        .bind(job_id)
        .execute(&mut **transaction)
        .await?,
    )?;
    Ok(TaskState::Failed)
}

pub(super) async fn promote_ready(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    now_ms: i64,
) -> Result<(), StoreError> {
    let candidates = sqlx::query(
        "SELECT id,spec_json FROM tasks WHERE job_id=? AND state='pending' ORDER BY task_key",
    )
    .bind(job_id)
    .fetch_all(&mut **transaction)
    .await?;
    for candidate in candidates {
        let spec_json: String = candidate.try_get("spec_json")?;
        let spec = serde_json::from_str::<CompiledTaskSpec>(&spec_json)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        let mut ready = true;
        for predecessor in spec.needs {
            let state: Option<String> =
                sqlx::query_scalar("SELECT state FROM tasks WHERE job_id=? AND task_key=?")
                    .bind(job_id)
                    .bind(predecessor)
                    .fetch_optional(&mut **transaction)
                    .await?;
            if !matches!(state.as_deref(), Some("succeeded" | "skipped")) {
                ready = false;
                break;
            }
        }
        if ready {
            sqlx::query(
                "UPDATE tasks SET state='ready',ready_at_ms=? WHERE id=? AND state='pending'",
            )
            .bind(now_ms)
            .bind(candidate.try_get::<String, _>("id")?)
            .execute(&mut **transaction)
            .await?;
        }
    }
    Ok(())
}
