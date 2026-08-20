use std::str::FromStr;

use super::{FinalAiUsage, StartAiUsage, Store, StoreError, UsageRecord};
use flori_core::{AiTool, AiUsageId, ErrorCode, UsageOrigin};

impl Store {
    pub async fn start_ai_usage(
        &self,
        usage: StartAiUsage<'_>,
        now_ms: i64,
    ) -> Result<UsageRecord, StoreError> {
        if usage.invocation_key.is_empty() || now_ms < 0 || usage.created_at_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let attempt_id = usage.attempt_id.to_string();
        let tool = ai_tool(usage.tool);
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let existing = sqlx::query!(
            r#"SELECT id AS 'id!',job_id AS 'job_id!',task_id AS 'task_id!',
                    state AS 'state!',tool AS 'tool!',model AS 'model!',effort AS 'effort!'
             FROM ai_usage WHERE attempt_id=? AND invocation_key=?"#,
            attempt_id,
            usage.invocation_key,
        )
        .fetch_optional(&mut *transaction)
        .await?;
        if let Some(row) = existing {
            let matches = row.state == "started"
                && row.job_id == usage.job_id.to_string()
                && row.task_id == usage.task_id.to_string()
                && row.tool == tool
                && row.model == usage.model
                && row.effort == usage.effort;
            transaction.rollback().await?;
            if matches {
                return Ok(UsageRecord {
                    id: parse_usage_id(&row.id)?,
                    is_final: false,
                    applied: false,
                });
            }
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }

        let id = usage.id.to_string();
        let job_id = usage.job_id.to_string();
        let task_id = usage.task_id.to_string();
        let inserted = sqlx::query!(
            r#"INSERT INTO ai_usage(
                 id,job_id,task_id,attempt_id,invocation_key,state,tool,model,effort,created_at_ms
             ) SELECT ?,j.id,t.id,a.id,?,'started',?,?,?,?
             FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id
             WHERE a.id=? AND a.task_id=? AND t.job_id=?
               AND a.state='leased' AND a.lease_expires_at_ms>?
               AND t.state='leased' AND t.current_attempt_id=a.id AND j.state='running'"#,
            id,
            usage.invocation_key,
            tool,
            usage.model,
            usage.effort,
            usage.created_at_ms,
            attempt_id,
            task_id,
            job_id,
            now_ms,
        )
        .execute(&mut *transaction)
        .await?;
        if inserted.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        transaction.commit().await?;
        Ok(UsageRecord {
            id: usage.id,
            is_final: false,
            applied: true,
        })
    }

    pub async fn finalize_ai_usage(
        &self,
        usage: FinalAiUsage<'_>,
    ) -> Result<UsageRecord, StoreError> {
        if usage.invocation_key.is_empty()
            || usage.finalized_at_ms < 0
            || [
                usage.input_tokens,
                usage.output_tokens,
                usage.cost_micros,
                usage.credits_micros,
            ]
            .into_iter()
            .flatten()
            .any(|value| value < 0)
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }

        let attempt_id = usage.attempt_id.to_string();
        let origin = usage_origin(usage.origin);
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query!(
            r#"SELECT id AS 'id!',state AS 'state!',tool AS 'tool!',origin,
                    created_at_ms AS 'created_at_ms!',
                    input_tokens,output_tokens,cost_micros,credits_micros
             FROM ai_usage WHERE attempt_id=? AND invocation_key=?"#,
            attempt_id,
            usage.invocation_key,
        )
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::UsageConflict));
        };
        valid_metrics(
            &row.tool,
            usage.origin,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_micros,
            usage.credits_micros,
        )?;
        if row.state == "final" {
            let matches = row.origin.as_deref() == Some(origin)
                && row.input_tokens == usage.input_tokens
                && row.output_tokens == usage.output_tokens
                && row.cost_micros == usage.cost_micros
                && row.credits_micros == usage.credits_micros;
            transaction.rollback().await?;
            if matches {
                return Ok(UsageRecord {
                    id: parse_usage_id(&row.id)?,
                    is_final: true,
                    applied: false,
                });
            }
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }
        if row.state != "started" {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        if usage.finalized_at_ms < row.created_at_ms {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }

        let updated = sqlx::query!(
            r#"UPDATE ai_usage SET state='final',origin=?,input_tokens=?,output_tokens=?,
                 cost_micros=?,credits_micros=?,finalized_at_ms=?
             WHERE attempt_id=? AND invocation_key=? AND state='started'"#,
            origin,
            usage.input_tokens,
            usage.output_tokens,
            usage.cost_micros,
            usage.credits_micros,
            usage.finalized_at_ms,
            attempt_id,
            usage.invocation_key,
        )
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }
        transaction.commit().await?;
        Ok(UsageRecord {
            id: parse_usage_id(&row.id)?,
            is_final: true,
            applied: true,
        })
    }
}

const fn ai_tool(tool: AiTool) -> &'static str {
    match tool {
        AiTool::QoderCli => "qoder_cli",
        AiTool::CodexCli => "codex_cli",
    }
}

const fn usage_origin(origin: UsageOrigin) -> &'static str {
    match origin {
        UsageOrigin::Observed => "observed",
        UsageOrigin::Estimated => "estimated",
        UsageOrigin::Unavailable => "unavailable",
    }
}

fn parse_usage_id(value: &str) -> Result<AiUsageId, StoreError> {
    AiUsageId::from_str(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn valid_metrics(
    tool: &str,
    origin: UsageOrigin,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    cost_micros: Option<i64>,
    credits_micros: Option<i64>,
) -> Result<(), StoreError> {
    let observed = origin == UsageOrigin::Observed;
    let valid = match tool {
        "qoder_cli" => {
            input_tokens.is_none()
                && output_tokens.is_none()
                && cost_micros.is_none()
                && (!observed || credits_micros.is_some())
        }
        "codex_cli" => {
            credits_micros.is_none()
                && (!observed || input_tokens.is_some() && output_tokens.is_some())
        }
        _ => return Err(StoreError::new(ErrorCode::CorruptState)),
    };
    if valid {
        Ok(())
    } else {
        Err(StoreError::new(ErrorCode::UsageConflict))
    }
}
