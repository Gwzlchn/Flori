use std::str::FromStr;

use flori_core::{
    AttemptId, CompiledTaskSpec, ErrorCode, Executor, JobId, RunnerId, SourceKind, TaskClaim,
    TaskId, TaskInputBindings,
};
use sqlx::Row;

use super::{
    super::{Store, StoreError},
    normalize::{RunnerInventory, load_inventory, supports_executor},
    resolve::{resolved_inputs, secret_inputs},
};

pub(super) mod server_log;

struct ExecutionSelection {
    model: Option<String>,
    effort: Option<String>,
    config_revision: u64,
}
impl Store {
    pub async fn poll_and_claim(
        &self,
        runner_id: RunnerId,
        now_ms: i64,
        lease_expires_at_ms: i64,
        artifact_download_base: &str,
    ) -> Result<Option<TaskClaim>, StoreError> {
        if now_ms < 0
            || lease_expires_at_ms <= now_ms
            || !valid_download_base(artifact_download_base)
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let runner_id_text = runner_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let inventory = load_inventory(&mut transaction, &runner_id_text).await?;
        let active: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM attempts WHERE runner_id=? AND state='leased' \
             AND lease_expires_at_ms>?",
        )
        .bind(&runner_id_text)
        .bind(now_ms)
        .fetch_one(&mut *transaction)
        .await?;
        if active >= inventory.max_concurrency {
            transaction.rollback().await?;
            return Ok(None);
        }
        let rows = sqlx::query(
            "SELECT t.id,t.job_id,t.task_key,t.executor,t.spec_json,t.input_bindings_json, \
             t.attempt_limit,t.timeout_ms,t.pinned_runner_id,t.selected_model,t.selected_effort, \
             t.runner_config_revision, \
             (SELECT count(*) FROM attempts a WHERE a.task_id=t.id) AS attempt_count, \
             s.id AS source_id,s.kind AS source_kind FROM tasks t JOIN jobs j ON j.id=t.job_id \
             JOIN sources s ON s.id=j.source_id \
             WHERE t.state='ready' AND j.state IN ('queued','running') \
             AND (t.ready_at_ms IS NULL OR t.ready_at_ms<=?) \
             AND t.executor NOT LIKE 'core.%' \
             AND (t.pinned_runner_id IS NULL OR t.pinned_runner_id=?) \
             ORDER BY t.ready_at_ms,t.task_key,t.id",
        )
        .bind(now_ms)
        .bind(&runner_id_text)
        .fetch_all(&mut *transaction)
        .await?;
        for row in rows {
            let spec: CompiledTaskSpec =
                serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
            let bindings: TaskInputBindings =
                serde_json::from_str(row.try_get("input_bindings_json")?).map_err(|_| corrupt())?;
            let executor_json = format!("\"{}\"", row.try_get::<String, _>("executor")?);
            let task_executor: Executor =
                serde_json::from_str(&executor_json).map_err(|_| corrupt())?;
            let source_kind_json = format!("\"{}\"", row.try_get::<String, _>("source_kind")?);
            let source_kind: SourceKind =
                serde_json::from_str(&source_kind_json).map_err(|_| corrupt())?;
            if task_executor != spec.executor
                || task_executor != bindings.executor()
                || !bindings.is_valid()
                || row.try_get::<i64, _>("timeout_ms")?
                    != i64::try_from(spec.timeout_ms)
                        .map_err(|_| StoreError::new(ErrorCode::CorruptState))?
                || row.try_get::<i64, _>("attempt_limit")? != i64::from(spec.retry) + 1
            {
                return Err(StoreError::new(ErrorCode::CorruptState));
            }
            let attempt_count: i64 = row.try_get("attempt_count")?;
            if attempt_count >= row.try_get::<i64, _>("attempt_limit")?
                || !supports_executor(task_executor, source_kind, &inventory)
                || !spec
                    .tags
                    .iter()
                    .all(|required| inventory.tags.binary_search(required).is_ok())
            {
                continue;
            }
            let Some(selection) =
                select_execution(&row, task_executor, &inventory, &runner_id_text)?
            else {
                continue;
            };
            let job_id_text: String = row.try_get("job_id")?;
            let task_id_text: String = row.try_get("id")?;
            let resolved = resolved_inputs(
                &mut transaction,
                &job_id_text,
                &bindings,
                artifact_download_base.trim_end_matches('/'),
            )
            .await?;
            let secrets = secret_inputs(&mut transaction, &job_id_text, task_executor).await?;
            let exec_id = AttemptId::generate();
            let attempt_no = u8::try_from(attempt_count + 1)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            server_log::start_attempt(
                &mut transaction,
                row.try_get::<String, _>("source_id")?
                    .parse()
                    .map_err(|_| corrupt())?,
                &job_id_text,
                &task_id_text,
                &runner_id_text,
                exec_id,
                attempt_no,
                &selection,
                &spec,
                now_ms,
                lease_expires_at_ms,
            )
            .await?;
            transaction.commit().await?;
            return Ok(Some(TaskClaim {
                job_id: JobId::from_str(&job_id_text).map_err(|_| corrupt())?,
                task_id: TaskId::from_str(&task_id_text).map_err(|_| corrupt())?,
                task_key: row.try_get("task_key")?,
                exec_id,
                attempt_no,
                executor: task_executor,
                timeout_ms: spec.timeout_ms,
                lease_expires_at_ms,
                resolved_inputs: resolved,
                output_declarations: spec.artifacts,
                model: selection.model,
                effort: selection.effort,
                runner_config_revision: selection.config_revision,
                secret_inputs: secrets,
            }));
        }
        transaction.rollback().await?;
        Ok(None)
    }
}
fn select_execution(
    row: &sqlx::sqlite::SqliteRow,
    executor: Executor,
    inventory: &RunnerInventory,
    runner_id: &str,
) -> Result<Option<ExecutionSelection>, StoreError> {
    let pinned: Option<String> = row.try_get("pinned_runner_id")?;
    let selected_model: Option<String> = row.try_get("selected_model")?;
    let selected_effort: Option<String> = row.try_get("selected_effort")?;
    let selected_revision: Option<i64> = row.try_get("runner_config_revision")?;
    let is_ai = matches!(
        executor,
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote
    );
    if !is_ai {
        return if pinned.is_none()
            && selected_model.is_none()
            && selected_effort.is_none()
            && selected_revision.is_none()
        {
            Ok(Some(ExecutionSelection {
                model: None,
                effort: None,
                config_revision: inventory.config_revision,
            }))
        } else {
            Err(StoreError::new(ErrorCode::CorruptState))
        };
    }
    let (model, effort, revision) =
        match (pinned, selected_model, selected_effort, selected_revision) {
            (Some(pinned), Some(model), Some(effort), Some(revision)) if pinned == runner_id => (
                model,
                effort,
                u64::try_from(revision).map_err(|_| corrupt())?,
            ),
            (None, None, None, None) => {
                let (Some(model), Some(effort)) = (
                    inventory.default_model.clone(),
                    inventory.default_effort.clone(),
                ) else {
                    return Ok(None);
                };
                (model, effort, inventory.config_revision)
            }
            _ => return Err(StoreError::new(ErrorCode::CorruptState)),
        };
    if inventory.ai_models.iter().any(|candidate| {
        candidate.model == model && candidate.efforts.binary_search(&effort).is_ok()
    }) {
        Ok(Some(ExecutionSelection {
            model: Some(model),
            effort: Some(effort),
            config_revision: revision,
        }))
    } else {
        Ok(None)
    }
}
fn valid_download_base(value: &str) -> bool {
    let value = value.trim_end_matches('/');
    if let Some(rest) = value.strip_prefix("https://") {
        return authority(rest).is_some();
    }
    let Some(rest) = value.strip_prefix("http://") else {
        return false;
    };
    let Some(authority) = authority(rest) else {
        return false;
    };
    authority == "localhost"
        || local_with_port(authority, "localhost")
        || authority == "127.0.0.1"
        || local_with_port(authority, "127.0.0.1")
        || authority == "[::1]"
        || local_with_port(authority, "[::1]")
}
fn authority(rest: &str) -> Option<&str> {
    let authority = rest.split('/').next()?;
    (!authority.is_empty()
        && !authority
            .bytes()
            .any(|byte| matches!(byte, b'@' | b'?' | b'#')))
    .then_some(authority)
}
fn local_with_port(authority: &str, host: &str) -> bool {
    authority
        .strip_prefix(host)
        .and_then(|suffix| suffix.strip_prefix(':'))
        .is_some_and(|port| !port.is_empty() && port.bytes().all(|byte| byte.is_ascii_digit()))
}
fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
