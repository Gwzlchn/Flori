use flori_core::{
    ArtifactWhen, AttemptAck, AttemptId, AttemptState, ErrorCode, FailAttemptRequest, RunnerId,
    UploadState,
};

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    terminal_common::{
        commit_uploads, delete_attempt_uploads, exact_moved, load_attempt_uploads, manifest,
        manifest_digest, required_present,
    },
    upload::active_attempt,
    upload_rule::declaration,
};

impl Store {
    pub async fn fail_authenticated_attempt(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        request: &FailAttemptRequest,
        now_ms: i64,
    ) -> Result<AttemptAck, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let active = active_attempt(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let uploads = load_attempt_uploads(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let mut committed = Vec::new();
        let mut discarded = Vec::new();
        for upload in &uploads {
            let (declared, _) = declaration(&active.spec, &upload.pending.artifact.name)?;
            if declared.when == ArtifactWhen::Always && upload.record.state() == UploadState::Moved
            {
                committed.push(upload);
            } else {
                discarded.push(upload);
            }
        }
        exact_moved(artifacts, &committed)?;
        required_present(&active, &committed, true)?;
        match (committed.is_empty(), request.manifest_sha256.as_ref()) {
            (true, None) => {}
            (false, Some(expected))
                if manifest_digest(&manifest(&active, attempt_id, &committed))? == *expected => {}
            _ => return Err(StoreError::new(ErrorCode::DigestMismatch)),
        }
        for upload in discarded {
            artifacts
                .discard(&upload.record)
                .map_err(|error| StoreError::new(error.code()))?;
        }
        commit_uploads(&mut transaction, &active, attempt_id, &committed, now_ms).await?;
        delete_attempt_uploads(&mut transaction, attempt_id).await?;
        super::super::scheduler::finish_failure(
            &mut transaction,
            &attempt_id.to_string(),
            &active.task_id.to_string(),
            &active.job_id.to_string(),
            active.attempt_no,
            active.attempt_limit,
            request.error_code,
            now_ms,
        )
        .await?;
        transaction.commit().await?;
        Ok(AttemptAck {
            exec_id: attempt_id,
            state: AttemptState::Failed,
        })
    }
}
