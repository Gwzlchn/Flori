use std::path::Path;

use flori_core::{AttemptId, ErrorCode, JobId, TaskId, TaskState, UploadState};

use crate::artifact::{NasArtifactStore, RecoveryAction};

use super::{
    super::{Store, StoreError},
    finish_success,
    validate::digest,
};

mod record;
mod reserve;

use record::{corrupt, decode, stored, validation_rows};
use reserve::PendingValidation;

impl Store {
    pub(in crate::sqlite) async fn resume_pdf_validation(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let bytes = self
            .pdf_evidence_bytes(artifacts, job_id)
            .await
            .map_err(|error| {
                if matches!(
                    error.code(),
                    ErrorCode::EvidenceInvalid
                        | ErrorCode::DigestMismatch
                        | ErrorCode::ArtifactInvalidPath
                        | ErrorCode::ArtifactTooLarge
                ) {
                    corrupt()
                } else {
                    error
                }
            })?;
        self.persist_pdf_validation(artifacts, job_id, task_id, attempt_id, None, &bytes, now_ms)
            .await
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) async fn persist_pdf_validation(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        active: Option<PendingValidation>,
        bytes: &[u8],
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let mut active = match active {
            Some(value) => value,
            None => {
                self.load_pending_validation(job_id, task_id, attempt_id)
                    .await?
            }
        };
        let size = u64::try_from(bytes.len()).map_err(|_| corrupt())?;
        if active.pending.artifact.size_bytes != size
            || active.pending.artifact.sha256 != digest(bytes)?
        {
            return Err(corrupt());
        }
        if active.record.state() == UploadState::Receiving {
            if artifacts
                .recovery_action(&active.record, true)
                .map_err(stored)?
                != RecoveryAction::ResumeReceiving
            {
                return Err(corrupt());
            }
            let before = active.record.received_bytes();
            artifacts
                .append(&mut active.record, 0, bytes)
                .map_err(stored)?;
            artifacts.verify_staging(&active.record).map_err(stored)?;
            let changed = sqlx::query(
                "UPDATE uploads SET received_bytes=?,state='verified',updated_at_ms=? \
                 WHERE id=? AND owner_kind='attempt' AND owner_id=? AND state='receiving' AND received_bytes=?",
            )
            .bind(i64::try_from(size).map_err(|_| corrupt())?)
            .bind(now_ms)
            .bind(active.upload_id.to_string())
            .bind(attempt_id.to_string())
            .bind(i64::try_from(before).map_err(|_| corrupt())?)
            .execute(&self.pool)
            .await?;
            if changed.rows_affected() != 1 {
                return Err(StoreError::new(ErrorCode::Conflict));
            }
            active
                .record
                .restore_progress(size, UploadState::Verified)
                .map_err(stored)?;
        }
        if active.record.state() == UploadState::Verified {
            artifacts.move_verified(&active.record).map_err(stored)?;
            let changed = sqlx::query(
                "UPDATE uploads SET state='moved',updated_at_ms=? WHERE id=? \
                 AND owner_kind='attempt' AND owner_id=? AND state='verified'",
            )
            .bind(now_ms)
            .bind(active.upload_id.to_string())
            .bind(attempt_id.to_string())
            .execute(&self.pool)
            .await?;
            if changed.rows_affected() != 1 {
                return Err(StoreError::new(ErrorCode::Conflict));
            }
            active
                .record
                .restore_progress(size, UploadState::Moved)
                .map_err(stored)?;
        }
        if active.record.state() != UploadState::Moved
            || artifacts
                .recovery_action(&active.record, true)
                .map_err(stored)?
                != RecoveryAction::RetryCommit
        {
            return Err(corrupt());
        }
        self.commit_pdf_validation(artifacts, job_id, task_id, attempt_id, now_ms)
            .await
    }

    async fn load_pending_validation(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
    ) -> Result<PendingValidation, StoreError> {
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let rows = validation_rows(&mut transaction, job_id, task_id, attempt_id).await?;
        if rows.len() != 1 {
            return Err(corrupt());
        }
        let loaded = decode(&rows[0])?;
        transaction.rollback().await?;
        Ok(loaded)
    }

    async fn commit_pdf_validation(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let rows = validation_rows(&mut transaction, job_id, task_id, attempt_id).await?;
        if rows.len() != 1 {
            return Err(corrupt());
        }
        let active = decode(&rows[0])?;
        if active.record.state() != UploadState::Moved
            || artifacts
                .recovery_action(&active.record, true)
                .map_err(stored)?
                != RecoveryAction::RetryCommit
        {
            return Err(corrupt());
        }
        let artifact = &active.pending.artifact;
        let file_name = Path::new(&artifact.relative_path)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(corrupt)?;
        sqlx::query(
            "INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind,media_type, \
             file_name,size_bytes,sha256,relative_path,retention,created_at_ms) SELECT ?,source_id,?,?,?, \
             'produced',?,'evidence','application/json',?,?,?,?,'published',? FROM jobs WHERE id=?",
        )
        .bind(active.pending.artifact_id.to_string())
        .bind(job_id.to_string())
        .bind(task_id.to_string())
        .bind(attempt_id.to_string())
        .bind(&artifact.name)
        .bind(file_name)
        .bind(i64::try_from(artifact.size_bytes).map_err(|_| corrupt())?)
        .bind(artifact.sha256.as_str())
        .bind(&artifact.relative_path)
        .bind(now_ms)
        .bind(job_id.to_string())
        .execute(&mut *transaction)
        .await?;
        sqlx::query("DELETE FROM uploads WHERE id=? AND owner_kind='attempt' AND owner_id=?")
            .bind(active.upload_id.to_string())
            .bind(attempt_id.to_string())
            .execute(&mut *transaction)
            .await?;
        finish_success(
            &mut transaction,
            &attempt_id.to_string(),
            &task_id.to_string(),
            &job_id.to_string(),
            now_ms,
        )
        .await?;
        transaction.commit().await?;
        Ok(TaskState::Succeeded)
    }
}
