use std::{collections::BTreeMap, str::FromStr};

use flori_core::{
    AiTool, AiUsageId, AiUsageState, AiUsageView, AttemptId, ErrorCode, JobId, TaskId, UsageOrigin,
};
use sqlx::{Row, sqlite::SqliteRow};

use super::super::{Store, StoreError};

impl Store {
    pub(super) async fn job_usage(
        &self,
        job_id: JobId,
    ) -> Result<BTreeMap<AttemptId, Vec<AiUsageView>>, StoreError> {
        let rows = sqlx::query(
            "SELECT u.id,u.job_id,u.task_id,u.attempt_id,a.task_id AS attempt_task_id, \
             t.job_id AS task_job_id,u.invocation_key,u.state,u.tool,u.model,u.effort,u.origin, \
             u.input_tokens,u.output_tokens,u.cost_micros,u.credits_micros,u.created_at_ms, \
             u.finalized_at_ms FROM ai_usage u \
             LEFT JOIN attempts a ON a.id=u.attempt_id LEFT JOIN tasks t ON t.id=u.task_id \
             WHERE u.job_id=? OR t.job_id=? OR a.task_id IN (SELECT id FROM tasks WHERE job_id=?) \
             ORDER BY u.attempt_id,u.created_at_ms,u.invocation_key",
        )
        .bind(job_id.to_string())
        .bind(job_id.to_string())
        .bind(job_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        let mut usage = BTreeMap::new();
        for row in &rows {
            if parse_id::<JobId>(row, "job_id")? != job_id
                || parse_id::<JobId>(row, "task_job_id")? != job_id
                || parse_id::<TaskId>(row, "task_id")?
                    != parse_id::<TaskId>(row, "attempt_task_id")?
            {
                return Err(StoreError::new(ErrorCode::CorruptState));
            }
            usage
                .entry(parse_id(row, "attempt_id")?)
                .or_insert_with(Vec::new)
                .push(parse_usage(row)?);
        }
        Ok(usage)
    }
}

fn parse_usage(row: &SqliteRow) -> Result<AiUsageView, StoreError> {
    let state = parse_usage_state(&row.try_get::<String, _>("state")?)?;
    let origin = row
        .try_get::<Option<String>, _>("origin")?
        .as_deref()
        .map(parse_usage_origin)
        .transpose()?;
    let created_at_ms = required_u64(row, "created_at_ms")?;
    let finalized_at_ms = optional_u64(row, "finalized_at_ms")?;
    let input_tokens = optional_u64(row, "input_tokens")?;
    let output_tokens = optional_u64(row, "output_tokens")?;
    let cost_micros = optional_u64(row, "cost_micros")?;
    let credits_micros = optional_u64(row, "credits_micros")?;
    let tool = parse_ai_tool(&row.try_get::<String, _>("tool")?)?;
    let no_metrics = input_tokens.is_none()
        && output_tokens.is_none()
        && cost_micros.is_none()
        && credits_micros.is_none();
    let valid_metrics = match (tool, origin) {
        (_, None | Some(UsageOrigin::Unavailable)) => no_metrics,
        (AiTool::QoderCli, Some(_)) => {
            input_tokens.is_none()
                && output_tokens.is_none()
                && cost_micros.is_none()
                && credits_micros.is_some()
        }
        (AiTool::CodexCli, Some(_)) => credits_micros.is_none(),
    };
    if !valid_metrics
        || matches!(state, AiUsageState::Started) != (origin.is_none() && finalized_at_ms.is_none())
        || finalized_at_ms.is_some_and(|value| value < created_at_ms)
    {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    Ok(AiUsageView {
        usage_id: parse_id::<AiUsageId>(row, "id")?,
        invocation_key: row.try_get("invocation_key")?,
        state,
        tool,
        model: row.try_get("model")?,
        effort: row.try_get("effort")?,
        origin,
        input_tokens,
        output_tokens,
        cost_micros,
        credits_micros,
        created_at_ms,
        finalized_at_ms,
    })
}

macro_rules! enum_parser {
    ($name:ident, $type:ty) => {
        fn $name(value: &str) -> Result<$type, StoreError> {
            let json = serde_json::to_string(value)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            serde_json::from_str(&json).map_err(|_| StoreError::new(ErrorCode::CorruptState))
        }
    };
}

enum_parser!(parse_ai_tool, AiTool);
enum_parser!(parse_usage_state, AiUsageState);
enum_parser!(parse_usage_origin, UsageOrigin);

fn parse_id<T: FromStr>(row: &SqliteRow, column: &str) -> Result<T, StoreError> {
    row.try_get::<String, _>(column)?
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u64(row: &SqliteRow, column: &str) -> Result<u64, StoreError> {
    u64::try_from(row.try_get::<i64, _>(column)?)
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn optional_u64(row: &SqliteRow, column: &str) -> Result<Option<u64>, StoreError> {
    row.try_get::<Option<i64>, _>(column)?
        .map(|value| u64::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState)))
        .transpose()
}
