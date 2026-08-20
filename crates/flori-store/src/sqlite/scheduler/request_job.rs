use std::{collections::BTreeSet, fmt::Write as _};

use flori_core::{
    CreateJobRequest, ErrorCode, JobId, JobTrigger, PromptSnapshotId, Sha256Digest, SourceId,
};
use flori_pipeline::compile;
use sha2::{Digest, Sha256};
use sqlx::Row;

use super::{
    super::{Store, StoreError},
    job::{CreateJob, freeze_tasks, source_context},
    snapshot::current_prompt_snapshot,
};

impl Store {
    pub async fn create_requested_job(
        &self,
        source_id: SourceId,
        request: &CreateJobRequest,
        now_ms: i64,
    ) -> Result<JobId, StoreError> {
        if request.request_key.is_empty() || now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let (source_kind, domain_id) = source_context(&self.pool, &source_id.to_string()).await?;
        let row = sqlx::query(
            "SELECT p.key,p.current_revision_id,r.yaml_sha256,r.yaml_text \
             FROM pipelines p JOIN pipeline_revisions r ON r.id=p.current_revision_id \
             WHERE p.id=?",
        )
        .bind(request.pipeline_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
        let pipeline_key: String = row.try_get("key")?;
        let yaml_text: String = row.try_get("yaml_text")?;
        let compilation = compile(&pipeline_key, yaml_text.as_bytes())
            .map_err(|_| StoreError::new(ErrorCode::PipelineInvalid))?;
        if compilation.sha256 != row.try_get::<String, _>("yaml_sha256")? {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        let frozen = freeze_tasks(&compilation, source_kind, request.inputs.translate)?;
        let required_prompts = frozen
            .iter()
            .filter(|task| task.included)
            .filter_map(|task| task.prompt_key.as_deref())
            .collect::<BTreeSet<_>>();
        let mut transaction = self.pool.begin().await?;
        let snapshot =
            current_prompt_snapshot(&mut transaction, domain_id, &required_prompts).await?;
        transaction.rollback().await?;
        let request_sha256 =
            digest(&serde_json::to_vec(request).map_err(|_| StoreError::new(ErrorCode::Internal))?);
        self.create_job(
            CreateJob {
                source_id,
                pipeline_revision_id: row
                    .try_get::<String, _>("current_revision_id")?
                    .parse()
                    .map_err(|_| StoreError::new(ErrorCode::CorruptState))?,
                trigger: JobTrigger::Initial,
                rerun_of_job_id: None,
                prompt_snapshot_id: PromptSnapshotId::generate(),
                prompt_snapshot: &snapshot,
                request_key: &request.request_key,
                request_sha256: request_sha256.as_str(),
                inputs: request.inputs,
                created_at_ms: now_ms,
            },
            &compilation,
        )
        .await
    }
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(output).expect("SHA-256 formatter is canonical")
}
