use std::str::FromStr;

use super::{
    super::{Store, StoreError},
    attempt::promote_ready,
    pipeline::valid_compilation,
    wire::{executor, job_trigger, source_kind},
};
use flori_core::{
    ErrorCode, JobId, JobTrigger, PipelineRevisionId, PromptSnapshotId, Sha256Digest, SourceId,
    SourceKind, TaskId,
};
use flori_pipeline::{Compilation, RuleCondition};
use sqlx::Row;

#[derive(Clone, Copy, Debug)]
pub struct CreateJob<'a> {
    pub source_id: SourceId,
    pub pipeline_revision_id: PipelineRevisionId,
    pub trigger: JobTrigger,
    pub rerun_of_job_id: Option<JobId>,
    pub prompt_snapshot_id: PromptSnapshotId,
    pub prompt_snapshot_sha256: &'a str,
    pub prompt_snapshot_json: &'a str,
    pub request_key: &'a str,
    pub request_sha256: &'a str,
    pub translate: bool,
    pub created_at_ms: i64,
}

impl Store {
    pub async fn create_job(
        &self,
        input: CreateJob<'_>,
        compilation: &Compilation,
    ) -> Result<JobId, StoreError> {
        validate_input(input, compilation)?;
        let source_id = input.source_id.to_string();
        let revision_id = input.pipeline_revision_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        if let Some(row) = sqlx::query(
            "SELECT id,source_id,pipeline_revision_id,request_sha256 FROM jobs WHERE request_key=?",
        )
        .bind(input.request_key)
        .fetch_optional(&mut *transaction)
        .await?
        {
            let matches = row.try_get::<String, _>("source_id")? == source_id
                && row.try_get::<String, _>("pipeline_revision_id")? == revision_id
                && row.try_get::<String, _>("request_sha256")? == input.request_sha256;
            let id: String = row.try_get("id")?;
            transaction.rollback().await?;
            return if matches {
                JobId::from_str(&id).map_err(|_| StoreError::new(ErrorCode::CorruptState))
            } else {
                Err(StoreError::new(ErrorCode::IdempotencyConflict))
            };
        }

        let row = sqlx::query(
            "SELECT s.kind AS source_kind,p.current_revision_id AS current_revision_id, \
                    r.yaml_sha256 AS revision_sha256 \
               FROM sources s,pipeline_revisions r \
               JOIN pipelines p ON p.id=r.pipeline_id \
              WHERE s.id=? AND r.id=?",
        )
        .bind(&source_id)
        .bind(&revision_id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::NotFound));
        };
        if row
            .try_get::<Option<String>, _>("current_revision_id")?
            .as_deref()
            != Some(revision_id.as_str())
            || row.try_get::<String, _>("revision_sha256")? != compilation.sha256
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::PipelineInvalid));
        }
        let source_kind_text: String = row.try_get("source_kind")?;
        let source_kind = parse_source_kind(&source_kind_text)?;
        validate_rerun(&mut transaction, input, &source_id).await?;
        let active: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM jobs WHERE source_id=? AND state IN ('queued','running')",
        )
        .bind(&source_id)
        .fetch_one(&mut *transaction)
        .await?;
        if active != 0 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::SourceBusy));
        }

        let job_id = JobId::generate();
        sqlx::query(
            "INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,rerun_of_job_id,state, \
             prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,request_key, \
             request_sha256,created_at_ms) VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)",
        )
        .bind(job_id.to_string())
        .bind(&source_id)
        .bind(&revision_id)
        .bind(job_trigger(input.trigger))
        .bind(input.rerun_of_job_id.map(|id| id.to_string()))
        .bind(input.prompt_snapshot_id.to_string())
        .bind(input.prompt_snapshot_sha256)
        .bind(input.prompt_snapshot_json)
        .bind(input.request_key)
        .bind(input.request_sha256)
        .bind(input.created_at_ms)
        .execute(&mut *transaction)
        .await?;

        for task_key in &compilation.pipeline.topological_order {
            let task = compilation
                .pipeline
                .tasks
                .get(task_key)
                .ok_or_else(|| StoreError::new(ErrorCode::PipelineInvalid))?;
            let spec_json: String =
                sqlx::query_scalar("SELECT value FROM json_each(?, '$.tasks') WHERE key=?")
                    .bind(&compilation.canonical_json)
                    .bind(task_key)
                    .fetch_one(&mut *transaction)
                    .await?;
            let input_bindings: String =
                sqlx::query_scalar("SELECT COALESCE(json_extract(?,'$.with'),'{}')")
                    .bind(&spec_json)
                    .fetch_one(&mut *transaction)
                    .await?;
            let included = included(&task.rules, source_kind, input.translate);
            sqlx::query(
                "INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json, \
                 state,attempt_limit,timeout_ms,finished_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
            )
            .bind(TaskId::generate().to_string())
            .bind(job_id.to_string())
            .bind(task_key)
            .bind(executor(task.executor))
            .bind(spec_json)
            .bind(input_bindings)
            .bind(if included { "pending" } else { "skipped" })
            .bind(i64::from(task.retry) + 1)
            .bind(
                i64::try_from(task.timeout_ms)
                    .map_err(|_| StoreError::new(ErrorCode::PipelineInvalid))?,
            )
            .bind((!included).then_some(input.created_at_ms))
            .execute(&mut *transaction)
            .await?;
        }
        promote_ready(&mut transaction, &job_id.to_string(), input.created_at_ms).await?;
        transaction.commit().await?;
        Ok(job_id)
    }
}

fn validate_input(input: CreateJob<'_>, compilation: &Compilation) -> Result<(), StoreError> {
    if input.request_key.is_empty()
        || input.created_at_ms < 0
        || Sha256Digest::parse(input.request_sha256).is_err()
        || Sha256Digest::parse(input.prompt_snapshot_sha256).is_err()
        || input.prompt_snapshot_json.is_empty()
        || !valid_compilation(compilation)
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    if matches!(input.trigger, JobTrigger::TaskRerun)
        || (matches!(input.trigger, JobTrigger::PipelineRerun) != input.rerun_of_job_id.is_some())
    {
        return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
    }
    Ok(())
}

async fn validate_rerun(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    input: CreateJob<'_>,
    source_id: &str,
) -> Result<(), StoreError> {
    let Some(rerun) = input.rerun_of_job_id else {
        return Ok(());
    };
    let previous_source: Option<String> =
        sqlx::query_scalar("SELECT source_id FROM jobs WHERE id=?")
            .bind(rerun.to_string())
            .fetch_optional(&mut **transaction)
            .await?;
    if previous_source.as_deref() != Some(source_id) {
        return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
    }
    Ok(())
}

fn included(rules: &[RuleCondition], kind: SourceKind, translate: bool) -> bool {
    rules.is_empty()
        || rules.iter().any(|rule| match rule {
            RuleCondition::SourceKind { equal, value } => *equal == (kind == *value),
            RuleCondition::JobTranslate { equal, value } => *equal == (translate == *value),
        })
}

fn parse_source_kind(value: &str) -> Result<SourceKind, StoreError> {
    [
        SourceKind::Arxiv,
        SourceKind::PdfUrl,
        SourceKind::PdfUpload,
        SourceKind::BilibiliVideo,
        SourceKind::BilibiliChannel,
        SourceKind::YoutubeVideo,
        SourceKind::YoutubeChannel,
        SourceKind::LocalVideo,
    ]
    .into_iter()
    .find(|kind| source_kind(*kind) == value)
    .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))
}
