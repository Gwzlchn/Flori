use std::{collections::BTreeMap, str::FromStr};

use flori_core::{
    ArtifactView, AttemptState, AttemptView, CompiledTaskSpec, ErrorCode, Executor, JobId,
    JobInputs, JobState, JobTrigger, JobView, Sha256Digest, SourceId, SourceKind, SourceView,
    TaskId, TaskState, TaskView,
};
use sqlx::{Row, sqlite::SqliteRow};

use super::super::{Store, StoreError};

impl Store {
    pub async fn get_source(&self, source_id: SourceId) -> Result<Option<SourceView>, StoreError> {
        let row = sqlx::query(
            "SELECT s.id,s.kind,s.canonical_ref,s.title,s.domain_id,s.current_job_id, \
             s.previous_job_id,c.source_id AS current_source_id,c.state AS current_state, \
             p.source_id AS previous_source_id,p.state AS previous_state FROM sources s \
             LEFT JOIN jobs c ON c.id=s.current_job_id LEFT JOIN jobs p ON p.id=s.previous_job_id \
             WHERE s.id=?",
        )
        .bind(source_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        row.as_ref().map(parse_source).transpose()
    }

    pub async fn get_job(&self, job_id: JobId) -> Result<Option<JobView>, StoreError> {
        let Some(row) = sqlx::query(
            "SELECT id,source_id,pipeline_revision_id,trigger,state,inputs_json,error_code, \
             error_message FROM jobs WHERE id=?",
        )
        .bind(job_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        else {
            return Ok(None);
        };
        let mut attempts = self.job_attempts(job_id).await?;
        let task_rows = sqlx::query(
            "SELECT id,task_key,executor,state,spec_json,current_attempt_id,pinned_runner_id, \
             selected_model,selected_effort,runner_config_revision,attempt_limit,timeout_ms, \
             error_code,error_message FROM tasks WHERE job_id=? ORDER BY task_key",
        )
        .bind(job_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        if task_rows.is_empty() {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        let mut tasks = Vec::with_capacity(task_rows.len());
        for task_row in &task_rows {
            let task_id = parse_id(task_row, "id")?;
            let task_attempts = attempts.remove(&task_id).unwrap_or_default();
            tasks.push(parse_task(task_row, task_attempts)?);
        }
        if !attempts.is_empty() {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        let source_id = parse_id(&row, "source_id")?;
        let artifacts = self.job_artifacts(job_id).await?;
        if artifacts.iter().any(|artifact| {
            artifact.source_id != source_id
                || !tasks.iter().any(|task| task.task_id == artifact.task_id)
        }) {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        Ok(Some(JobView {
            job_id: parse_id(&row, "id")?,
            source_id,
            pipeline_revision_id: parse_id(&row, "pipeline_revision_id")?,
            trigger: parse_job_trigger(&row.try_get::<String, _>("trigger")?)?,
            state: parse_job_state(&row.try_get::<String, _>("state")?)?,
            inputs: parse_job_inputs(&row.try_get::<String, _>("inputs_json")?)?,
            error_code: parse_optional_error(&row, "error_code")?,
            error_message: row.try_get("error_message")?,
            tasks,
            artifacts,
        }))
    }

    async fn job_attempts(
        &self,
        job_id: JobId,
    ) -> Result<BTreeMap<TaskId, Vec<AttemptView>>, StoreError> {
        let rows = sqlx::query(
            "SELECT a.id,a.task_id,a.attempt_no,a.runner_id,a.state,a.model,a.effort, \
             a.runner_config_revision,a.lease_expires_at_ms,a.last_log_sequence,a.started_at_ms, \
             a.finished_at_ms,a.error_code,a.error_message FROM attempts a JOIN tasks t \
             ON t.id=a.task_id WHERE t.job_id=? ORDER BY t.task_key,a.attempt_no",
        )
        .bind(job_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        let mut attempts = BTreeMap::new();
        for row in &rows {
            let attempt = parse_attempt(row)?;
            attempts
                .entry(attempt.task_id)
                .or_insert_with(Vec::new)
                .push(attempt);
        }
        Ok(attempts)
    }

    async fn job_artifacts(&self, job_id: JobId) -> Result<Vec<ArtifactView>, StoreError> {
        let rows = sqlx::query(
            "SELECT a.id,a.source_id,a.job_id,a.task_id,a.name,a.kind,a.media_type,a.size_bytes, \
             a.sha256 FROM artifacts a JOIN tasks t ON t.id=a.task_id \
             WHERE a.job_id=? ORDER BY t.task_key,a.name,a.id",
        )
        .bind(job_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(parse_artifact).collect()
    }
}

fn parse_source(row: &SqliteRow) -> Result<SourceView, StoreError> {
    let source_id = parse_id(row, "id")?;
    let current_job_id = parse_optional_id(row, "current_job_id")?;
    let previous_job_id = parse_optional_id(row, "previous_job_id")?;
    validate_pointer(row, source_id, current_job_id, "current")?;
    validate_pointer(row, source_id, previous_job_id, "previous")?;
    Ok(SourceView {
        source_id,
        kind: parse_source_kind(&row.try_get::<String, _>("kind")?)?,
        canonical_ref: row.try_get("canonical_ref")?,
        title: row.try_get("title")?,
        domain_id: parse_id(row, "domain_id")?,
        current_job_id,
        previous_job_id,
    })
}

fn validate_pointer(
    row: &SqliteRow,
    source_id: SourceId,
    job_id: Option<JobId>,
    prefix: &str,
) -> Result<(), StoreError> {
    let linked_source: Option<String> = row.try_get(format!("{prefix}_source_id").as_str())?;
    let state: Option<String> = row.try_get(format!("{prefix}_state").as_str())?;
    match (job_id, linked_source, state) {
        (None, None, None) => Ok(()),
        (Some(_), Some(linked), Some(state))
            if parse_text_id::<SourceId>(&linked)? == source_id && state == "succeeded" =>
        {
            Ok(())
        }
        _ => Err(StoreError::new(ErrorCode::CorruptState)),
    }
}

fn parse_task(row: &SqliteRow, attempts: Vec<AttemptView>) -> Result<TaskView, StoreError> {
    let executor = parse_executor(&row.try_get::<String, _>("executor")?)?;
    let spec = parse_task_spec(&row.try_get::<String, _>("spec_json")?)?;
    let attempt_limit = required_u8(row, "attempt_limit")?;
    if spec.executor != executor
        || spec.retry.checked_add(1) != Some(attempt_limit)
        || spec.timeout_ms != required_u64(row, "timeout_ms")?
    {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    let current_attempt_id = parse_optional_id(row, "current_attempt_id")?;
    if current_attempt_id.is_some_and(|id| !attempts.iter().any(|attempt| attempt.attempt_id == id))
    {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    Ok(TaskView {
        task_id: parse_id(row, "id")?,
        task_key: row.try_get("task_key")?,
        executor,
        state: parse_task_state(&row.try_get::<String, _>("state")?)?,
        spec,
        current_attempt_id,
        pinned_runner_id: parse_optional_id(row, "pinned_runner_id")?,
        selected_model: row.try_get("selected_model")?,
        selected_effort: row.try_get("selected_effort")?,
        runner_config_revision: optional_u64(row, "runner_config_revision")?,
        error_code: parse_optional_error(row, "error_code")?,
        error_message: row.try_get("error_message")?,
        attempts,
    })
}

fn parse_attempt(row: &SqliteRow) -> Result<AttemptView, StoreError> {
    Ok(AttemptView {
        attempt_id: parse_id(row, "id")?,
        task_id: parse_id(row, "task_id")?,
        attempt_no: required_u32(row, "attempt_no")?,
        runner_id: parse_optional_id(row, "runner_id")?,
        state: parse_attempt_state(&row.try_get::<String, _>("state")?)?,
        model: row.try_get("model")?,
        effort: row.try_get("effort")?,
        runner_config_revision: optional_u64(row, "runner_config_revision")?,
        lease_expires_at_ms: required_u64(row, "lease_expires_at_ms")?,
        last_log_sequence: required_u64(row, "last_log_sequence")?,
        started_at_ms: required_u64(row, "started_at_ms")?,
        finished_at_ms: optional_u64(row, "finished_at_ms")?,
        error_code: parse_optional_error(row, "error_code")?,
        error_message: row.try_get("error_message")?,
    })
}

fn parse_artifact(row: &SqliteRow) -> Result<ArtifactView, StoreError> {
    Ok(ArtifactView {
        artifact_id: parse_id(row, "id")?,
        source_id: parse_id(row, "source_id")?,
        job_id: parse_id(row, "job_id")?,
        task_id: parse_id(row, "task_id")?,
        name: row.try_get("name")?,
        kind: parse_artifact_kind(&row.try_get::<String, _>("kind")?)?,
        media_type: row.try_get("media_type")?,
        size_bytes: required_u64(row, "size_bytes")?,
        sha256: Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?,
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

enum_parser!(parse_source_kind, SourceKind);
enum_parser!(parse_job_trigger, JobTrigger);
enum_parser!(parse_job_state, JobState);
enum_parser!(parse_task_state, TaskState);
enum_parser!(parse_attempt_state, AttemptState);
enum_parser!(parse_executor, Executor);
enum_parser!(parse_artifact_kind, flori_core::ArtifactKind);
enum_parser!(parse_error, ErrorCode);

fn parse_optional_error(row: &SqliteRow, column: &str) -> Result<Option<ErrorCode>, StoreError> {
    row.try_get::<Option<String>, _>(column)?
        .as_deref()
        .map(parse_error)
        .transpose()
}

fn parse_job_inputs(value: &str) -> Result<JobInputs, StoreError> {
    serde_json::from_str(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn parse_task_spec(value: &str) -> Result<CompiledTaskSpec, StoreError> {
    serde_json::from_str(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn parse_id<T: FromStr>(row: &SqliteRow, column: &str) -> Result<T, StoreError> {
    parse_text_id(&row.try_get::<String, _>(column)?)
}

fn parse_optional_id<T: FromStr>(row: &SqliteRow, column: &str) -> Result<Option<T>, StoreError> {
    row.try_get::<Option<String>, _>(column)?
        .as_deref()
        .map(parse_text_id)
        .transpose()
}

fn parse_text_id<T: FromStr>(value: &str) -> Result<T, StoreError> {
    value
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u8(row: &SqliteRow, column: &str) -> Result<u8, StoreError> {
    u8::try_from(row.try_get::<i64, _>(column)?)
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u32(row: &SqliteRow, column: &str) -> Result<u32, StoreError> {
    u32::try_from(row.try_get::<i64, _>(column)?)
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
