use std::{collections::BTreeSet, str::FromStr};

use flori_core::{
    ErrorCode, JobId, JobInputs, JobTrigger, PIPELINE_COMPILER_VERSION, PipelineRevisionId,
    PromptSnapshotId, RerunJobRequest, RerunMode, SourceId,
};
use flori_pipeline::{Compilation, compile};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    job::{CreateJob, freeze_tasks, source_context},
    pipeline::valid_compilation,
    rerun::intent_digest,
    snapshot::current_prompt_snapshot,
};

struct RerunContext {
    source_id: SourceId,
    revision_id: PipelineRevisionId,
    inputs: JobInputs,
    compilation: Compilation,
}

impl Store {
    pub async fn rerun_requested_job(
        &self,
        artifacts: &NasArtifactStore,
        base_job_id: JobId,
        request: &RerunJobRequest,
        now_ms: i64,
    ) -> Result<JobId, StoreError> {
        if request.request_key.is_empty() || now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let context = self.rerun_context(base_job_id).await?;
        match request.mode {
            RerunMode::FromTask => {
                self.rerun_from_task(
                    artifacts,
                    base_job_id,
                    request,
                    &context.compilation,
                    now_ms,
                )
                .await
            }
            RerunMode::Pipeline => {
                if request.from_task_key.is_some() || request.ai_selection.is_some() {
                    return Err(StoreError::new(ErrorCode::InvalidRequest));
                }
                self.create_pipeline_rerun(base_job_id, request, context, now_ms)
                    .await
            }
        }
    }

    async fn rerun_context(&self, base_job_id: JobId) -> Result<RerunContext, StoreError> {
        let row = sqlx::query(
            "SELECT j.source_id,j.inputs_json,p.key,p.current_revision_id,r.compiler_version, \
             r.yaml_sha256,r.yaml_text FROM jobs j \
             JOIN pipeline_revisions base ON base.id=j.pipeline_revision_id \
             JOIN pipelines p ON p.id=base.pipeline_id \
             LEFT JOIN pipeline_revisions r ON r.id=p.current_revision_id WHERE j.id=?",
        )
        .bind(base_job_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
        let revision_id = row
            .try_get::<Option<String>, _>("current_revision_id")?
            .ok_or_else(pipeline_invalid)?
            .parse()
            .map_err(|_| pipeline_invalid())?;
        let compiler_version = row
            .try_get::<Option<i64>, _>("compiler_version")?
            .ok_or_else(pipeline_invalid)?;
        let key = row.try_get::<String, _>("key")?;
        let yaml = row
            .try_get::<Option<String>, _>("yaml_text")?
            .ok_or_else(pipeline_invalid)?;
        let compilation = compile(&key, yaml.as_bytes()).map_err(|_| pipeline_invalid())?;
        if compiler_version != i64::from(PIPELINE_COMPILER_VERSION)
            || !valid_compilation(&compilation)
            || row.try_get::<Option<String>, _>("yaml_sha256")?.as_deref()
                != Some(compilation.sha256.as_str())
        {
            return Err(pipeline_invalid());
        }
        let inputs_json: String = row.try_get("inputs_json")?;
        let inputs: JobInputs = serde_json::from_str(&inputs_json).map_err(|_| corrupt())?;
        if serde_json::to_string(&inputs).map_err(|_| corrupt())? != inputs_json {
            return Err(corrupt());
        }
        Ok(RerunContext {
            source_id: SourceId::from_str(row.try_get("source_id")?).map_err(|_| corrupt())?,
            revision_id,
            inputs,
            compilation,
        })
    }

    async fn create_pipeline_rerun(
        &self,
        base_job_id: JobId,
        request: &RerunJobRequest,
        context: RerunContext,
        now_ms: i64,
    ) -> Result<JobId, StoreError> {
        let (kind, domain_id) = source_context(&self.pool, &context.source_id.to_string()).await?;
        let frozen = freeze_tasks(&context.compilation, kind, context.inputs.translate)?;
        let required_prompts = frozen
            .iter()
            .filter(|task| task.included)
            .filter_map(|task| task.prompt_key.as_deref())
            .collect::<BTreeSet<_>>();
        let mut transaction = self.pool.begin().await?;
        let snapshot =
            current_prompt_snapshot(&mut transaction, domain_id, &required_prompts).await?;
        transaction.rollback().await?;
        let request_sha256 = intent_digest(base_job_id, request, &context.compilation)?;
        self.create_job(
            CreateJob {
                source_id: context.source_id,
                pipeline_revision_id: context.revision_id,
                trigger: JobTrigger::PipelineRerun,
                rerun_of_job_id: Some(base_job_id),
                prompt_snapshot_id: PromptSnapshotId::generate(),
                prompt_snapshot: &snapshot,
                request_key: &request.request_key,
                request_sha256: &request_sha256,
                inputs: context.inputs,
                created_at_ms: now_ms,
            },
            &context.compilation,
        )
        .await
    }
}

fn pipeline_invalid() -> StoreError {
    StoreError::new(ErrorCode::PipelineInvalid)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
