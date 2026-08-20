use std::collections::BTreeSet;

use flori_core::{AiModels, AiRunnerSelection, ErrorCode, Executor, RunnerTool, RunnerTools};
use sqlx::{Row, Sqlite, Transaction};

use super::super::StoreError;
use super::job::FrozenTask;

pub(super) async fn validate_ai_selection(
    transaction: &mut Transaction<'_, Sqlite>,
    selection: Option<&AiRunnerSelection>,
    rerun: &BTreeSet<String>,
    tasks: &[FrozenTask],
) -> Result<(), StoreError> {
    let Some(selection) = selection else {
        return Ok(());
    };
    tasks
        .iter()
        .find(|task| task.key == selection.task_key)
        .filter(|task| rerun.contains(&task.key) && is_ai(task.spec.executor))
        .ok_or_else(|| StoreError::new(ErrorCode::RerunBoundaryInvalid))?;
    let row = sqlx::query(
        "SELECT state,config_revision,tools_json,ai_models_json FROM runners WHERE id=?",
    )
    .bind(selection.runner_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::RunnerUnavailable))?;
    let tools_json: String = row.try_get("tools_json")?;
    let models_json: String = row.try_get("ai_models_json")?;
    let tools: RunnerTools = serde_json::from_str(&tools_json).map_err(|_| corrupt())?;
    let models: AiModels = serde_json::from_str(&models_json).map_err(|_| corrupt())?;
    if row.try_get::<String, _>("state")? != "enabled"
        || u64::try_from(row.try_get::<i64, _>("config_revision")?).map_err(|_| corrupt())?
            != selection.runner_config_revision
        || serde_json::to_string(&tools).map_err(|_| corrupt())? != tools_json
        || serde_json::to_string(&models).map_err(|_| corrupt())? != models_json
        || !tools
            .iter()
            .any(|tool| matches!(tool.tool, RunnerTool::QoderCli | RunnerTool::CodexCli))
        || !models.iter().any(|model| {
            model.model == selection.model && model.efforts.contains(&selection.effort)
        })
    {
        return Err(StoreError::new(ErrorCode::RunnerUnavailable));
    }
    Ok(())
}

const fn is_ai(executor: Executor) -> bool {
    matches!(
        executor,
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote
    )
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
