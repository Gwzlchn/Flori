use std::{collections::BTreeMap, io::Read};

use flori_core::{
    ArtifactId, ArtifactKind, DocumentStructure, ErrorCode, EvidenceLocator, EvidenceManifest,
    PendingMaterializedArtifact, PendingTaskCommit, Sha256Digest, UploadId,
};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, UploadRecord};

use super::{super::StoreError, rerun_artifact::digest_bytes};

pub(super) async fn freeze_rewritten_metadata(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    tasks: &[PendingTaskCommit],
    planned: &mut [PendingMaterializedArtifact],
) -> Result<(), StoreError> {
    let id_map = source_id_map(planned);
    for artifact in planned.iter_mut().filter(|artifact| {
        matches!(
            artifact.kind,
            ArtifactKind::DocumentStructure | ArtifactKind::Evidence
        )
    }) {
        let source = source_record(transaction, artifact.source_artifact_id).await?;
        let bytes = read_verified(artifacts, &source)?;
        let rewritten = rewrite(artifact.kind, &id_map, &bytes)?.ok_or_else(corrupt)?;
        let task = tasks
            .iter()
            .find(|task| task.task_id == artifact.task_id)
            .ok_or_else(corrupt)?;
        let declaration = task
            .spec
            .artifacts
            .iter()
            .find(|item| item.name == artifact.name && item.kind == artifact.kind)
            .ok_or_else(corrupt)?;
        artifact.size_bytes = rewritten
            .len()
            .try_into()
            .map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?;
        if artifact.size_bytes > declaration.max_bytes {
            return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
        }
        artifact.sha256 = digest_bytes(&rewritten);
    }
    Ok(())
}

pub(super) fn rewritten_bytes(
    artifacts: &NasArtifactStore,
    pending: &[PendingMaterializedArtifact],
    artifact: &PendingMaterializedArtifact,
    source: &UploadRecord,
) -> Result<Option<Vec<u8>>, StoreError> {
    let bytes = match artifact.kind {
        ArtifactKind::DocumentStructure | ArtifactKind::Evidence => {
            let source = read_verified(artifacts, source)?;
            rewrite(artifact.kind, &source_id_map(pending), &source)?
        }
        ArtifactKind::SourceOriginal
        | ArtifactKind::Figure
        | ArtifactKind::TableRegion
        | ArtifactKind::Translation
        | ArtifactKind::Subtitle
        | ArtifactKind::Transcript
        | ArtifactKind::Keyframe
        | ArtifactKind::Danmaku
        | ArtifactKind::PartsManifest
        | ArtifactKind::SubscriptionManifest
        | ArtifactKind::MechanicalNote
        | ArtifactKind::SmartNote
        | ArtifactKind::Summary
        | ArtifactKind::Terms
        | ArtifactKind::TaskLog
        | ArtifactKind::AiAudit => None,
    };
    if let Some(bytes) = &bytes
        && (bytes.len() as u64 != artifact.size_bytes || digest_bytes(bytes) != artifact.sha256)
    {
        return Err(corrupt());
    }
    Ok(bytes)
}

async fn source_record(
    transaction: &mut Transaction<'_, Sqlite>,
    artifact_id: ArtifactId,
) -> Result<UploadRecord, StoreError> {
    let row = sqlx::query("SELECT relative_path,size_bytes,sha256 FROM artifacts WHERE id=?")
        .bind(artifact_id.to_string())
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or_else(corrupt)?;
    let size: u64 = row
        .try_get::<i64, _>("size_bytes")?
        .try_into()
        .map_err(|_| corrupt())?;
    UploadRecord::new(
        UploadId::generate(),
        "source",
        row.try_get::<String, _>("relative_path")?,
        size,
        Sha256Digest::parse(row.try_get::<String, _>("sha256")?).map_err(|_| corrupt())?,
        "source",
        size,
    )
    .map_err(|_| corrupt())
}

fn read_verified(
    artifacts: &NasArtifactStore,
    source: &UploadRecord,
) -> Result<Vec<u8>, StoreError> {
    let size = source.expected_size_bytes();
    let mut file = artifacts
        .open_verified_range(
            source.final_relative_path(),
            size,
            source.expected_sha256(),
            0,
            size,
        )
        .map_err(|error| StoreError::new(error.code()))?;
    let capacity = size
        .try_into()
        .map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?;
    let mut bytes = Vec::with_capacity(capacity);
    file.read_to_end(&mut bytes)
        .map_err(|_| StoreError::new(ErrorCode::StorageUnavailable))?;
    Ok(bytes)
}

fn source_id_map(planned: &[PendingMaterializedArtifact]) -> BTreeMap<ArtifactId, ArtifactId> {
    planned
        .iter()
        .filter(|artifact| artifact.kind == ArtifactKind::SourceOriginal)
        .map(|artifact| (artifact.source_artifact_id, artifact.artifact_id))
        .collect()
}

fn rewrite(
    kind: ArtifactKind,
    id_map: &BTreeMap<ArtifactId, ArtifactId>,
    bytes: &[u8],
) -> Result<Option<Vec<u8>>, StoreError> {
    match kind {
        ArtifactKind::DocumentStructure => {
            let mut document: DocumentStructure =
                serde_json::from_slice(bytes).map_err(|_| evidence_invalid())?;
            document.source_artifact_id = remap(id_map, document.source_artifact_id)?;
            document.validate().map_err(|_| evidence_invalid())?;
            serde_json::to_vec(&document)
                .map(Some)
                .map_err(|_| StoreError::new(ErrorCode::Internal))
        }
        ArtifactKind::Evidence => {
            let mut evidence: EvidenceManifest =
                serde_json::from_slice(bytes).map_err(|_| evidence_invalid())?;
            for item in &mut evidence.items {
                item.source_artifact_id = remap(id_map, item.source_artifact_id)?;
                if !matches!(item.locator, EvidenceLocator::Pdf { .. }) {
                    return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
                }
            }
            evidence
                .validate_structure()
                .map_err(|_| evidence_invalid())?;
            serde_json::to_vec(&evidence)
                .map(Some)
                .map_err(|_| StoreError::new(ErrorCode::Internal))
        }
        _ => Ok(None),
    }
}

fn remap(
    id_map: &BTreeMap<ArtifactId, ArtifactId>,
    source: ArtifactId,
) -> Result<ArtifactId, StoreError> {
    id_map.get(&source).copied().ok_or_else(evidence_invalid)
}

fn evidence_invalid() -> StoreError {
    StoreError::new(ErrorCode::EvidenceInvalid)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
