use std::io::Read;

use flori_core::{
    ArtifactId, ArtifactKind, AttemptId, CompiledTaskSpec, DocumentStructure, ErrorCode, JobId,
    Sha256Digest, TaskId, TaskState, TermsManifest, validate_pdf_evidence,
};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::super::{Store, StoreError};

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
        let bytes = self.pdf_evidence_bytes(artifacts, job_id).await?;
        let size =
            u64::try_from(bytes.len()).map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?;
        if size > declaration.max_bytes {
            return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
        }
        let digest = digest(&bytes)?;
        let pending = self
            .reserve_pdf_validation(
                artifacts,
                source_id,
                job_id,
                task_id,
                attempt_id,
                &declaration,
                size,
                &digest,
                now_ms,
            )
            .await?;
        self.persist_pdf_validation(
            artifacts,
            job_id,
            task_id,
            attempt_id,
            Some(pending),
            &bytes,
            now_ms,
        )
        .await
    }

    pub(super) async fn pdf_evidence_bytes(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
    ) -> Result<Vec<u8>, StoreError> {
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
        serde_json::to_vec(&manifest).map_err(|_| StoreError::new(ErrorCode::Internal))
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

pub(super) fn digest(bytes: &[u8]) -> Result<Sha256Digest, StoreError> {
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
