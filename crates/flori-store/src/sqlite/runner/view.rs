use std::str::FromStr;

use flori_core::{
    AiModels, ErrorCode, RegisterRunnerRequest, RunnerId, RunnerState, RunnerTags, RunnerTools,
    RunnerView,
};
use sqlx::{Row, sqlite::SqliteRow};

use super::{
    super::{Store, StoreError},
    normalize::{capabilities, identifier, tags_json},
};

impl Store {
    pub async fn list_runners(
        &self,
        now_ms: i64,
        online_window_ms: u64,
    ) -> Result<Vec<RunnerView>, StoreError> {
        if now_ms < 0 || online_window_ms == 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let rows = sqlx::query(
            "SELECT r.id,r.name,r.state,r.config_revision,r.max_concurrency,r.tags_json, \
             r.tools_json,r.ai_models_json,r.default_model,r.default_effort,r.last_seen_at_ms, \
             (SELECT count(*) FROM attempts a WHERE a.runner_id=r.id AND a.state='leased' \
              AND a.lease_expires_at_ms>?) AS active_attempts FROM runners r ORDER BY r.name,r.id",
        )
        .bind(now_ms)
        .fetch_all(&self.pool)
        .await?;
        rows.iter()
            .map(|row| parse_runner(row, now_ms, online_window_ms))
            .collect()
    }
}

fn parse_runner(
    row: &SqliteRow,
    now_ms: i64,
    online_window_ms: u64,
) -> Result<RunnerView, StoreError> {
    let state = parse_state(row.try_get("state")?)?;
    let tags_raw: String = row.try_get("tags_json")?;
    let tools_raw: String = row.try_get("tools_json")?;
    let models_raw: String = row.try_get("ai_models_json")?;
    let tags: RunnerTags = serde_json::from_str(&tags_raw).map_err(|_| corrupt())?;
    let tools: RunnerTools = serde_json::from_str(&tools_raw).map_err(|_| corrupt())?;
    let ai_models: AiModels = serde_json::from_str(&models_raw).map_err(|_| corrupt())?;
    let normalized = capabilities(&RegisterRunnerRequest {
        tools: tools.clone(),
        ai_models: ai_models.clone(),
    })
    .map_err(|_| corrupt())?;
    if tags_json(&tags).map_err(|_| corrupt())? != tags_raw
        || normalized.tools_json != tools_raw
        || normalized.ai_models_json != models_raw
    {
        return Err(corrupt());
    }
    let default_model: Option<String> = row.try_get("default_model")?;
    let default_effort: Option<String> = row.try_get("default_effort")?;
    validate_defaults(
        state,
        &ai_models,
        default_model.as_deref(),
        default_effort.as_deref(),
    )?;
    let last_seen_at_ms = optional_u64(row, "last_seen_at_ms")?;
    let now = u64::try_from(now_ms).map_err(|_| corrupt())?;
    let active_attempts = required_u16(row, "active_attempts")?;
    let max_concurrency = required_u16(row, "max_concurrency")?;
    if active_attempts > max_concurrency {
        return Err(corrupt());
    }
    Ok(RunnerView {
        runner_id: RunnerId::from_str(row.try_get("id")?).map_err(|_| corrupt())?,
        name: row.try_get("name")?,
        state,
        online: state == RunnerState::Enabled
            && last_seen_at_ms
                .is_some_and(|seen| seen <= now && now.saturating_sub(seen) <= online_window_ms),
        config_revision: required_u64(row, "config_revision")?,
        max_concurrency,
        active_attempts,
        tags,
        tools,
        ai_models,
        default_model,
        default_effort,
        last_seen_at_ms,
    })
}

fn validate_defaults(
    state: RunnerState,
    models: &AiModels,
    model: Option<&str>,
    effort: Option<&str>,
) -> Result<(), StoreError> {
    match (model, effort) {
        (None, None) => Ok(()),
        (Some(model), Some(effort))
            if identifier(model)
                && identifier(effort)
                && (state == RunnerState::Disabled
                    || models.iter().any(|entry| {
                        entry.model == model
                            && entry.efforts.iter().any(|candidate| candidate == effort)
                    })) =>
        {
            Ok(())
        }
        _ => Err(corrupt()),
    }
}

fn parse_state(value: &str) -> Result<RunnerState, StoreError> {
    let json = serde_json::to_string(value).map_err(|_| corrupt())?;
    serde_json::from_str(&json).map_err(|_| corrupt())
}

fn required_u16(row: &SqliteRow, column: &str) -> Result<u16, StoreError> {
    u16::try_from(row.try_get::<i64, _>(column)?).map_err(|_| corrupt())
}

fn required_u64(row: &SqliteRow, column: &str) -> Result<u64, StoreError> {
    u64::try_from(row.try_get::<i64, _>(column)?).map_err(|_| corrupt())
}

fn optional_u64(row: &SqliteRow, column: &str) -> Result<Option<u64>, StoreError> {
    row.try_get::<Option<i64>, _>(column)?
        .map(|value| u64::try_from(value).map_err(|_| corrupt()))
        .transpose()
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
