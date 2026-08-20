use flori_core::{
    AiTool, AiUsageId, AiUsageState, AttemptId, ErrorCode, RegisterRunnerRequest, RunnerId,
    RunnerTool, RunnerTools, UsageAck, UsageUpdate,
};
use sqlx::Row;

use super::{
    super::super::{FinalAiUsage, StartAiUsage, Store, StoreError},
    normalize::capabilities,
};

impl Store {
    pub async fn apply_usage_update(
        &self,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        update: &UsageUpdate,
        now_ms: i64,
    ) -> Result<UsageAck, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let row = sqlx::query(
            "SELECT a.runner_id,a.model,a.effort,r.tools_json,t.id AS task_id,t.executor,j.id AS job_id \
             FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
             JOIN runners r ON r.id=a.runner_id \
             WHERE a.id=?",
        )
        .bind(attempt_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
        if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
            != Some(runner_id.to_string().as_str())
        {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        let record = match update {
            UsageUpdate::Started {
                invocation_key,
                tool,
                model,
                effort,
            } => {
                let tools_json: String = row.try_get("tools_json")?;
                let tools: RunnerTools =
                    serde_json::from_str(&tools_json).map_err(|_| corrupt())?;
                let normalized = capabilities(&RegisterRunnerRequest {
                    tools: tools.clone(),
                    ai_models: Vec::new(),
                })?;
                if !row.try_get::<String, _>("executor")?.starts_with("ai.")
                    || row.try_get::<Option<String>, _>("model")?.as_deref() != Some(model.as_str())
                    || row.try_get::<Option<String>, _>("effort")?.as_deref()
                        != Some(effort.as_str())
                    || normalized.tools_json != tools_json
                    || !tools.iter().any(|entry| entry.tool == runner_tool(*tool))
                {
                    return Err(StoreError::new(ErrorCode::UsageConflict));
                }
                self.start_ai_usage(
                    StartAiUsage {
                        id: AiUsageId::generate(),
                        job_id: row
                            .try_get::<String, _>("job_id")?
                            .parse()
                            .map_err(|_| corrupt())?,
                        task_id: row
                            .try_get::<String, _>("task_id")?
                            .parse()
                            .map_err(|_| corrupt())?,
                        attempt_id,
                        invocation_key,
                        tool: *tool,
                        model,
                        effort,
                        created_at_ms: now_ms,
                    },
                    now_ms,
                )
                .await?
            }
            UsageUpdate::Final {
                invocation_key,
                origin,
                input_tokens,
                output_tokens,
                cost_micros,
                credits_micros,
            } => {
                self.finalize_ai_usage(FinalAiUsage {
                    attempt_id,
                    invocation_key,
                    origin: *origin,
                    input_tokens: checked(*input_tokens)?,
                    output_tokens: checked(*output_tokens)?,
                    cost_micros: checked(*cost_micros)?,
                    credits_micros: checked(*credits_micros)?,
                    finalized_at_ms: now_ms,
                })
                .await?
            }
        };
        Ok(UsageAck {
            usage_id: record.id,
            state: if record.is_final {
                AiUsageState::Final
            } else {
                AiUsageState::Started
            },
        })
    }
}

const fn runner_tool(tool: AiTool) -> RunnerTool {
    match tool {
        AiTool::QoderCli => RunnerTool::QoderCli,
        AiTool::CodexCli => RunnerTool::CodexCli,
    }
}

fn checked(value: Option<u64>) -> Result<Option<i64>, StoreError> {
    value
        .map(|value| i64::try_from(value).map_err(|_| StoreError::new(ErrorCode::InvalidRequest)))
        .transpose()
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
