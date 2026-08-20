use std::{fmt::Write, path::Path, str::FromStr};

use flori_core::{
    ArtifactKind, ArtifactManifest, ArtifactWhen, AttemptId, CompiledTaskSpec, ErrorCode,
    PendingAttemptUpload, RunnerId, Sha256Digest, UploadId, UploadState,
};
use sha2::{Digest, Sha256};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord, task_artifact_path};

use super::{
    super::StoreError,
    upload::{ActiveAttempt, ActiveUpload, load_upload},
    upload_rule::{declaration, retention},
};

pub(super) async fn load_attempt_uploads(
    transaction: &mut Transaction<'_, Sqlite>,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<Vec<ActiveUpload>, StoreError> {
    let ids: Vec<String> = sqlx::query_scalar(
        "SELECT id FROM uploads WHERE owner_kind='attempt' AND owner_id=? \
         AND commit_json IS NOT NULL ORDER BY name",
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
        declaration.required
            && (!failure
                || (declaration.when == ArtifactWhen::Always
                    && declaration.kind != ArtifactKind::TaskLog))
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

pub(super) async fn cleanup_failed_uploads(
    pool: &sqlx::SqlitePool,
    artifacts: &NasArtifactStore,
    runner_id: RunnerId,
    attempt_id: AttemptId,
) -> Result<(), StoreError> {
    loop {
        let mut transaction = pool.begin_with("BEGIN IMMEDIATE").await?;
        let state = sqlx::query("SELECT runner_id,state FROM attempts WHERE id=?")
            .bind(attempt_id.to_string())
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
        if state.try_get::<Option<String>, _>("runner_id")?.as_deref()
            != Some(runner_id.to_string().as_str())
            || state.try_get::<String, _>("state")? != "failed"
        {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        let row = sqlx::query(
            "SELECT u.id,u.commit_json,u.name,u.target_id,u.staging_path,u.final_relative_path, \
             u.expected_size_bytes,u.expected_sha256,u.received_bytes,u.state,t.spec_json, \
             t.id AS task_id,j.id AS job_id,j.source_id FROM uploads u JOIN attempts a \
             ON a.id=u.owner_id JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
             WHERE u.owner_kind='attempt' AND u.owner_id=? ORDER BY u.id LIMIT 1",
        )
        .bind(attempt_id.to_string())
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Ok(());
        };
        let upload_id: UploadId = row
            .try_get::<String, _>("id")?
            .parse()
            .map_err(|_| corrupt())?;
        let spec: CompiledTaskSpec =
            serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
        let record = cleanup_record(&row, upload_id, &spec)?;
        artifacts
            .discard(&record)
            .map_err(|error| StoreError::new(error.code()))?;
        let deleted =
            sqlx::query("DELETE FROM uploads WHERE id=? AND owner_kind='attempt' AND owner_id=?")
                .bind(upload_id.to_string())
                .bind(attempt_id.to_string())
                .execute(&mut *transaction)
                .await?;
        if deleted.rows_affected() != 1 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
    }
}

fn cleanup_record(
    row: &sqlx::sqlite::SqliteRow,
    upload_id: UploadId,
    spec: &CompiledTaskSpec,
) -> Result<UploadRecord, StoreError> {
    let name: String = row.try_get("name")?;
    let (declared, basename) = declaration(spec, &name)?;
    let commit = row.try_get::<Option<String>, _>("commit_json")?;
    let (expected_size, expected_sha, declared_name, final_path) = if let Some(json) = commit {
        let pending: PendingAttemptUpload = serde_json::from_str(&json).map_err(|_| corrupt())?;
        if pending.artifact_id.to_string() != row.try_get::<String, _>("target_id")?
            || pending.artifact.name != name
            || pending.artifact.kind != declared.kind
            || pending.declaration_name != declared.name
            || !declared
                .kind
                .accepts_media_type(&pending.artifact.media_type)
            || i64::try_from(pending.artifact.size_bytes).map_err(|_| invalid())?
                != row.try_get::<i64, _>("expected_size_bytes")?
            || pending.artifact.sha256.as_str() != row.try_get::<String, _>("expected_sha256")?
        {
            return Err(corrupt());
        }
        (
            pending.artifact.size_bytes,
            pending.artifact.sha256,
            pending.declaration_name,
            pending.artifact.relative_path,
        )
    } else {
        if declared.kind != ArtifactKind::TaskLog
            || row.try_get::<String, _>("state")? != "receiving"
            || row.try_get::<i64, _>("expected_size_bytes")?
                != i64::try_from(declared.max_bytes).map_err(|_| invalid())?
        {
            return Err(corrupt());
        }
        let final_path = task_artifact_path(
            row.try_get::<String, _>("source_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("job_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("task_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("target_id")?
                .parse()
                .map_err(|_| corrupt())?,
            &basename,
        )
        .map_err(|error| StoreError::new(error.code()))?;
        (
            row.try_get::<i64, _>("expected_size_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            Sha256Digest::parse(row.try_get::<String, _>("expected_sha256")?)
                .map_err(|_| corrupt())?,
            declared.name.clone(),
            final_path,
        )
    };
    if final_path != row.try_get::<String, _>("final_relative_path")? {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        name,
        final_path,
        expected_size,
        expected_sha,
        &declared_name,
        declared.max_bytes,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(
            row.try_get::<i64, _>("received_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            serde_json::from_str(&format!("\"{}\"", row.try_get::<String, _>("state")?))
                .map_err(|_| corrupt())?,
        )
        .map_err(|_| corrupt())?;
    if record.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok(record)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
