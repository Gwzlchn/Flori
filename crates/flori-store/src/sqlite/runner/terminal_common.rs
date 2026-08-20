use std::{fmt::Write, path::Path, str::FromStr};

use flori_core::{
    ArtifactManifest, ArtifactManifestEntry, ArtifactWhen, AttemptId, ErrorCode, RunnerId,
    Sha256Digest, UploadId, UploadState,
};
use sha2::{Digest, Sha256};
use sqlx::{Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, RecoveryAction};

use super::{
    super::StoreError,
    upload::{ActiveAttempt, ActiveUpload, load_upload},
    upload_rule::retention,
};

pub(super) async fn load_attempt_uploads(
    transaction: &mut Transaction<'_, Sqlite>,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<Vec<ActiveUpload>, StoreError> {
    let ids: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM uploads WHERE owner_kind='attempt' AND owner_id=? ORDER BY name",
    )
    .bind(attempt_id.to_string())
    .fetch_all(&mut **transaction)
    .await?;
    let mut uploads = Vec::with_capacity(ids.len());
    for id in ids {
        uploads.push(
            load_upload(
                transaction,
                runner_id,
                UploadId::from_str(&id).map_err(|_| corrupt())?,
                now_ms,
            )
            .await?,
        );
    }
    Ok(uploads)
}

pub(super) fn exact_moved(
    artifacts: &NasArtifactStore,
    uploads: &[&ActiveUpload],
) -> Result<(), StoreError> {
    for upload in uploads {
        if upload.record.state() != UploadState::Moved
            || artifacts
                .recovery_action(&upload.record, true)
                .map_err(|error| StoreError::new(error.code()))?
                != RecoveryAction::RetryCommit
        {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
    }
    Ok(())
}

pub(super) fn required_present(
    active: &ActiveAttempt,
    uploads: &[&ActiveUpload],
    failure: bool,
) -> Result<(), StoreError> {
    for declaration in active.spec.artifacts.iter().filter(|declaration| {
        declaration.required && (!failure || declaration.when == ArtifactWhen::Always)
    }) {
        if !uploads
            .iter()
            .any(|upload| upload.pending.declaration_name == declaration.name)
        {
            return Err(StoreError::new(ErrorCode::ArtifactUndeclared));
        }
    }
    Ok(())
}

pub(super) fn manifest(
    active: &ActiveAttempt,
    attempt_id: AttemptId,
    uploads: &[&ActiveUpload],
) -> ArtifactManifest {
    ArtifactManifest::new(
        active.job_id,
        active.task_id,
        attempt_id,
        uploads
            .iter()
            .map(|upload| upload.pending.artifact.clone())
            .collect(),
    )
}

pub(super) fn manifest_digest(manifest: &ArtifactManifest) -> Result<Sha256Digest, StoreError> {
    let bytes = serde_json::to_vec(manifest).map_err(|_| corrupt())?;
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(value).map_err(|_| corrupt())
}

pub(super) async fn commit_uploads(
    transaction: &mut Transaction<'_, Sqlite>,
    active: &ActiveAttempt,
    attempt_id: AttemptId,
    uploads: &[&ActiveUpload],
    now_ms: i64,
) -> Result<(), StoreError> {
    for upload in uploads {
        let artifact = &upload.pending.artifact;
        if !artifact.kind.accepts_media_type(&artifact.media_type) {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        let file_name = Path::new(&artifact.relative_path)
            .file_name()
            .and_then(|value| value.to_str())
            .ok_or_else(corrupt)?;
        let kind = serde_json::to_string(&artifact.kind).map_err(|_| corrupt())?;
        sqlx::query(
            "INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind, \
             media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) \
             VALUES(?,?,?,?,?,'produced',?,?,?,?,?,?,?,?,?)",
        )
        .bind(upload.pending.artifact_id.to_string())
        .bind(active.source_id.to_string())
        .bind(active.job_id.to_string())
        .bind(active.task_id.to_string())
        .bind(attempt_id.to_string())
        .bind(&artifact.name)
        .bind(kind.trim_matches('"'))
        .bind(&artifact.media_type)
        .bind(file_name)
        .bind(i64::try_from(artifact.size_bytes).map_err(|_| invalid())?)
        .bind(artifact.sha256.as_str())
        .bind(&artifact.relative_path)
        .bind(retention(artifact.kind))
        .bind(now_ms)
        .execute(&mut **transaction)
        .await?;
    }
    Ok(())
}

pub(super) async fn delete_attempt_uploads(
    transaction: &mut Transaction<'_, Sqlite>,
    attempt_id: AttemptId,
) -> Result<(), StoreError> {
    sqlx::query("DELETE FROM uploads WHERE owner_kind='attempt' AND owner_id=?")
        .bind(attempt_id.to_string())
        .execute(&mut **transaction)
        .await?;
    Ok(())
}

pub(super) fn entries_manifest(
    job_id: flori_core::JobId,
    task_id: flori_core::TaskId,
    attempt_id: AttemptId,
    entries: Vec<ArtifactManifestEntry>,
) -> ArtifactManifest {
    ArtifactManifest::new(job_id, task_id, attempt_id, entries)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
