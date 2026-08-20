use std::io::Read;

use flori_core::{
    ArtifactId, ArtifactKind, AttemptId, CompiledTaskSpec, DocumentStructure, ErrorCode, JobId,
    Sha256Digest, TaskId, TaskState, TermsManifest, UploadId, UploadState, validate_pdf_evidence,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, UploadRecord, task_artifact_path};

use super::{
    super::{Store, StoreError},
    attempt::promote_ready,
};

struct InputArtifact {
    id: ArtifactId,
    kind: ArtifactKind,
    size: u64,
    sha256: Sha256Digest,
    path: String,
}

impl Store {
    pub async fn validate_pdf_job(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let (source_id, declaration) = self.validate_task(job_id, task_id).await?;
        let inputs = self.validation_inputs(job_id).await?;
        let document: DocumentStructure = serde_json::from_str(&read_text(
            artifacts,
            one(&inputs, ArtifactKind::DocumentStructure)?,
        )?)
        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?;
        let smart_note = read_text(artifacts, one(&inputs, ArtifactKind::SmartNote)?)?;
        let summary = read_text(artifacts, one(&inputs, ArtifactKind::Summary)?)?;
        let terms: TermsManifest =
            serde_json::from_str(&read_text(artifacts, one(&inputs, ArtifactKind::Terms)?)?)
                .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?;
        let original = one(&inputs, ArtifactKind::SourceOriginal)?;
        if document.source_artifact_id != original.id {
            return Err(StoreError::new(ErrorCode::EvidenceInvalid));
        }
        let manifest = validate_pdf_evidence(&document, &terms, &smart_note, &summary)
            .map_err(StoreError::new)?;
        let bytes =
            serde_json::to_vec(&manifest).map_err(|_| StoreError::new(ErrorCode::Internal))?;
        let size =
            u64::try_from(bytes.len()).map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?;
        if size > declaration.max_bytes {
            return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
        }
        let artifact_id = ArtifactId::generate();
        let file_name = declaration.path.rsplit('/').next().ok_or_else(corrupt)?;
        let relative = task_artifact_path(source_id, job_id, task_id, artifact_id, file_name)
            .map_err(|error| StoreError::new(error.code()))?;
        let digest = digest(&bytes)?;
        let mut upload = UploadRecord::new(
            UploadId::generate(),
            &declaration.name,
            &relative,
            size,
            digest.clone(),
            &declaration.name,
            declaration.max_bytes,
        )
        .map_err(|error| StoreError::new(error.code()))?;
        artifacts
            .append(&mut upload, 0, &bytes)
            .map_err(|error| StoreError::new(error.code()))?;
        artifacts
            .verify_staging(&upload)
            .map_err(|error| StoreError::new(error.code()))?;
        upload
            .restore_progress(size, UploadState::Verified)
            .map_err(|error| StoreError::new(error.code()))?;
        artifacts
            .move_verified(&upload)
            .map_err(|error| StoreError::new(error.code()))?;
        upload
            .restore_progress(size, UploadState::Moved)
            .map_err(|error| StoreError::new(error.code()))?;
        let result = self
            .commit_validation(
                job_id,
                task_id,
                attempt_id,
                artifact_id,
                &declaration.name,
                &relative,
                size,
                &digest,
                now_ms,
            )
            .await;
        if result.is_err() {
            artifacts
                .discard(&upload)
                .map_err(|error| StoreError::new(error.code()))?;
        }
        result
    }

    async fn validate_task(
        &self,
        job_id: JobId,
        task_id: TaskId,
    ) -> Result<(flori_core::SourceId, flori_core::ArtifactDeclaration), StoreError> {
        let row = sqlx::query(
            "SELECT j.source_id,j.state AS job_state,t.state AS task_state,t.executor,t.spec_json \
             FROM jobs j JOIN tasks t ON t.job_id=j.id WHERE j.id=? AND t.id=?",
        )
        .bind(job_id.to_string())
        .bind(task_id.to_string())
        .fetch_optional(&self.pool)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
        let spec: CompiledTaskSpec =
            serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
        let mut declarations = spec.artifacts.into_iter().filter(|item| {
            item.kind == ArtifactKind::Evidence && item.required && item.max_files.is_none()
        });
        let declaration = declarations
            .next()
            .filter(|_| declarations.next().is_none())
            .ok_or_else(corrupt)?;
        if row.try_get::<String, _>("job_state")? != "running"
            || row.try_get::<String, _>("task_state")? != "ready"
            || row.try_get::<String, _>("executor")? != "core.validate"
        {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        Ok((
            row.try_get::<String, _>("source_id")?
                .parse()
                .map_err(|_| corrupt())?,
            declaration,
        ))
    }

    async fn validation_inputs(&self, job_id: JobId) -> Result<Vec<InputArtifact>, StoreError> {
        let rows = sqlx::query(
            "SELECT a.id,a.kind,a.size_bytes,a.sha256,a.relative_path FROM artifacts a \
             JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? AND a.kind IN \
             ('source_original','document_structure','smart_note','summary','terms') AND \
             ((a.origin='produced' AND t.state='succeeded' AND a.attempt_id=t.current_attempt_id) \
              OR (a.origin='materialized' AND t.state='skipped' AND a.attempt_id IS NULL)) ORDER BY a.kind,a.id",
        )
        .bind(job_id.to_string())
        .fetch_all(&self.pool)
        .await?;
        rows.into_iter()
            .map(|row| {
                let kind: ArtifactKind =
                    serde_json::from_str(&format!("\"{}\"", row.try_get::<String, _>("kind")?))
                        .map_err(|_| corrupt())?;
                Ok(InputArtifact {
                    id: row
                        .try_get::<String, _>("id")?
                        .parse()
                        .map_err(|_| corrupt())?,
                    kind,
                    size: row
                        .try_get::<i64, _>("size_bytes")?
                        .try_into()
                        .map_err(|_| corrupt())?,
                    sha256: Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
                        .map_err(|_| corrupt())?,
                    path: row.try_get("relative_path")?,
                })
            })
            .collect()
    }

    #[allow(clippy::too_many_arguments)]
    async fn commit_validation(
        &self,
        job_id: JobId,
        task_id: TaskId,
        attempt_id: AttemptId,
        artifact_id: ArtifactId,
        name: &str,
        relative: &str,
        size: u64,
        sha256: &Sha256Digest,
        now_ms: i64,
    ) -> Result<TaskState, StoreError> {
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let state: Option<(String, String)> = sqlx::query_as(
            "SELECT j.state,t.state FROM jobs j JOIN tasks t ON t.job_id=j.id WHERE j.id=? AND t.id=?",
        )
        .bind(job_id.to_string()).bind(task_id.to_string()).fetch_optional(&mut *transaction).await?;
        if state
            .as_ref()
            .map(|(job, task)| (job.as_str(), task.as_str()))
            != Some(("running", "ready"))
        {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        sqlx::query("INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms,last_log_sequence,started_at_ms,finished_at_ms) VALUES(?,?,1,NULL,'succeeded',?,0,?,?)")
            .bind(attempt_id.to_string()).bind(task_id.to_string()).bind(now_ms).bind(now_ms).bind(now_ms)
            .execute(&mut *transaction).await?;
        sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) SELECT ?,source_id,?,?,?,'produced',?,'evidence','application/json','evidence.json',?,?,?,'published',? FROM jobs WHERE id=?")
            .bind(artifact_id.to_string()).bind(job_id.to_string()).bind(task_id.to_string()).bind(attempt_id.to_string())
            .bind(name).bind(i64::try_from(size).map_err(|_| corrupt())?).bind(sha256.as_str()).bind(relative).bind(now_ms).bind(job_id.to_string())
            .execute(&mut *transaction).await?;
        sqlx::query("UPDATE tasks SET state='succeeded',current_attempt_id=?,started_at_ms=?,finished_at_ms=? WHERE id=? AND state='ready'")
            .bind(attempt_id.to_string()).bind(now_ms).bind(now_ms).bind(task_id.to_string()).execute(&mut *transaction).await?;
        promote_ready(&mut transaction, &job_id.to_string(), now_ms).await?;
        transaction.commit().await?;
        Ok(TaskState::Succeeded)
    }
}

fn one(values: &[InputArtifact], kind: ArtifactKind) -> Result<&InputArtifact, StoreError> {
    let mut matching = values.iter().filter(|item| item.kind == kind);
    let item = matching
        .next()
        .ok_or_else(|| StoreError::new(ErrorCode::EvidenceInvalid))?;
    if matching.next().is_some() {
        return Err(StoreError::new(ErrorCode::EvidenceInvalid));
    }
    Ok(item)
}

fn read_text(artifacts: &NasArtifactStore, input: &InputArtifact) -> Result<String, StoreError> {
    let file = artifacts
        .open_verified_range(&input.path, input.size, &input.sha256, 0, input.size)
        .map_err(|error| StoreError::new(error.code()))?;
    let mut text = String::new();
    file.take(input.size)
        .read_to_string(&mut text)
        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?;
    Ok(text)
}

fn digest(bytes: &[u8]) -> Result<Sha256Digest, StoreError> {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| StoreError::new(ErrorCode::Internal))
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
