use std::path::Path;

use flori_core::{AttemptId, ErrorCode, JobId, TaskId, TaskState, UploadState};

use crate::artifact::{NasArtifactStore, RecoveryAction};

use super::{
    super::{Store, StoreError},
    finish_success,
};

mod reserve;

use reserve::PendingValidation;

impl Store {
    #[allow(clippy::too_many_arguments)]
    pub(super) async fn persist_pdf_validation(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        mut active: PendingValidation,
        bytes: &[u8],
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let size = u64::try_from(bytes.len()).map_err(|_| corrupt())?;
        artifacts
            .append(&mut active.record, 0, bytes)
            .map_err(stored)?;
        artifacts.verify_staging(&active.record).map_err(stored)?;
        let changed = sqlx::query(
            "UPDATE uploads SET received_bytes=?,state='verified',updated_at_ms=? \
             WHERE id=? AND owner_kind='attempt' AND owner_id=? AND state='receiving' AND received_bytes=0",
        )
        .bind(i64::try_from(size).map_err(|_| corrupt())?)
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
            .restore_progress(size, UploadState::Verified)
            .map_err(stored)?;
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
        if artifacts
            .recovery_action(&active.record, true)
            .map_err(stored)?
            != RecoveryAction::RetryCommit
        {
            return Err(corrupt());
        }
        self.commit_pdf_validation(job_id, task_id, attempt_id, active, now_ms)
            .await
    }

    async fn commit_pdf_validation(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        active: PendingValidation,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let artifact = &active.pending.artifact;
        let file_name = Path::new(&artifact.relative_path)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(corrupt)?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let state: Option<(String, String, String)> = sqlx::query_as(
            "SELECT j.state,t.state,a.state FROM jobs j JOIN tasks t ON t.job_id=j.id \
             JOIN attempts a ON a.id=t.current_attempt_id WHERE j.id=? AND t.id=? AND a.id=?",
        )
        .bind(job_id.to_string())
        .bind(task_id.to_string())
        .bind(attempt_id.to_string())
        .fetch_optional(&mut *transaction)
        .await?;
        if state
            .as_ref()
            .map(|value| (&*value.0, &*value.1, &*value.2))
            != Some(("running", "leased", "leased"))
        {
            return Err(corrupt());
        }
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

fn stored(error: crate::artifact::ArtifactStoreError) -> StoreError {
    StoreError::new(if error.code() == ErrorCode::StorageUnavailable {
        ErrorCode::StorageUnavailable
    } else {
        ErrorCode::CorruptState
    })
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
