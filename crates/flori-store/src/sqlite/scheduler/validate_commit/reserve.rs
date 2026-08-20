use std::path::Path;

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactManifestEntry, AttemptId, ErrorCode,
    JobId, PendingAttemptUpload, Sha256Digest, SourceId, TaskId, UploadId,
};

use crate::artifact::{NasArtifactStore, UploadRecord, task_artifact_path};

use super::super::super::super::{Store, StoreError};

pub(in crate::sqlite::scheduler) struct PendingValidation {
    pub(super) upload_id: UploadId,
    pub(super) pending: PendingAttemptUpload,
    pub(super) record: UploadRecord,
}

impl Store {
    #[allow(clippy::too_many_arguments)]
    pub(in crate::sqlite::scheduler) async fn reserve_pdf_validation(
        &self,
        artifacts: &NasArtifactStore,
        source_id: SourceId,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        declaration: &ArtifactDeclaration,
        size: u64,
        sha256: &Sha256Digest,
        now_ms: i64,
    ) -> Result<PendingValidation, StoreError> {
        let artifact_id = ArtifactId::generate();
        let upload_id = UploadId::generate();
        let file_name = Path::new(&declaration.path)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(corrupt)?;
        let relative = task_artifact_path(source_id, job_id, task_id, artifact_id, file_name)
            .map_err(|error| StoreError::new(error.code()))?;
        let pending = PendingAttemptUpload {
            artifact_id,
            declaration_name: declaration.name.clone(),
            artifact: ArtifactManifestEntry {
                name: declaration.name.clone(),
                kind: ArtifactKind::Evidence,
                media_type: "application/json".to_owned(),
                size_bytes: size,
                sha256: sha256.clone(),
                relative_path: relative.clone(),
            },
        };
        let record = UploadRecord::new(
            upload_id,
            &declaration.name,
            &relative,
            size,
            sha256.clone(),
            &declaration.name,
            declaration.max_bytes,
        )
        .map_err(stored)?;
        artifacts.validate_upload(&record).map_err(stored)?;

        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let inserted = sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms) SELECT ?,t.id,1,NULL,'leased',?,0,? FROM tasks t \
             JOIN jobs j ON j.id=t.job_id WHERE t.id=? AND t.job_id=? AND t.state='ready' \
             AND t.executor='core.validate' AND j.state='running'",
        )
        .bind(attempt_id.to_string())
        .bind(now_ms)
        .bind(now_ms)
        .bind(task_id.to_string())
        .bind(job_id.to_string())
        .execute(&mut *transaction)
        .await?;
        if inserted.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        let updated = sqlx::query(
            "UPDATE tasks SET state='leased',current_attempt_id=?,started_at_ms=COALESCE(started_at_ms,?) \
             WHERE id=? AND job_id=? AND state='ready'",
        )
        .bind(attempt_id.to_string())
        .bind(now_ms)
        .bind(task_id.to_string())
        .bind(job_id.to_string())
        .execute(&mut *transaction)
        .await?;
        if updated.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
             final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) \
             VALUES(?,'attempt',?,?,?,?,?,?,?, ?,0,'receiving',?,?)",
        )
        .bind(upload_id.to_string())
        .bind(attempt_id.to_string())
        .bind(serde_json::to_string(&pending).map_err(|_| corrupt())?)
        .bind(&declaration.name)
        .bind(artifact_id.to_string())
        .bind(record.staging_relative_path().to_string_lossy().as_ref())
        .bind(relative)
        .bind(i64::try_from(size).map_err(|_| corrupt())?)
        .bind(sha256.as_str())
        .bind(now_ms)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(PendingValidation {
            upload_id,
            pending,
            record,
        })
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
