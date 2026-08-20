use std::str::FromStr;

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactManifestEntry, AttemptId,
    CompiledTaskSpec, ErrorCode, PendingAttemptUpload, RunnerId, StartUploadRequest,
    StartUploadResponse, UploadId, UploadState,
};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, UploadRecord, retained_artifact_path, task_artifact_path};

use super::super::{Store, StoreError};

pub(super) struct ActiveUpload {
    pub record: UploadRecord,
    pub pending: PendingAttemptUpload,
}

struct ActiveAttempt {
    source_id: flori_core::SourceId,
    job_id: flori_core::JobId,
    task_id: flori_core::TaskId,
    spec: CompiledTaskSpec,
}

impl Store {
    pub async fn start_attempt_upload(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        request: &StartUploadRequest,
        now_ms: i64,
    ) -> Result<StartUploadResponse, StoreError> {
        if now_ms < 0 || request.media_type.is_empty() || request.media_type.contains(['\r', '\n'])
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let active = active_attempt(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let (declaration, basename) = declaration(&active.spec, &request.name)?;
        if let Some(row) = sqlx::query(
            "SELECT id FROM uploads WHERE owner_kind='attempt' AND owner_id=? AND name=?",
        )
        .bind(attempt_id.to_string())
        .bind(&request.name)
        .fetch_optional(&mut *transaction)
        .await?
        {
            let upload_id = UploadId::from_str(row.try_get("id")?).map_err(|_| corrupt())?;
            let loaded = load_upload(&mut transaction, runner_id, upload_id, now_ms).await?;
            if loaded.pending.artifact.media_type != request.media_type
                || loaded.pending.artifact.size_bytes != request.size_bytes
                || loaded.pending.artifact.sha256 != request.sha256
            {
                return Err(StoreError::new(ErrorCode::Conflict));
            }
            transaction.rollback().await?;
            return Ok(StartUploadResponse {
                upload_id,
                received_bytes: loaded.record.received_bytes(),
                artifact: loaded.pending.artifact,
            });
        }
        let count: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM uploads WHERE owner_kind='attempt' AND owner_id=? \
             AND (name=? OR name LIKE ?)",
        )
        .bind(attempt_id.to_string())
        .bind(&declaration.name)
        .bind(format!("{}/%", declaration.name))
        .fetch_one(&mut *transaction)
        .await?;
        let limit = i64::from(declaration.max_files.unwrap_or(1));
        if count >= limit {
            return Err(StoreError::new(ErrorCode::ArtifactTooLarge));
        }
        let upload_id = UploadId::generate();
        let artifact_id = ArtifactId::generate();
        let retention = retention(declaration.kind);
        let final_path = if retention == "source" {
            retained_artifact_path(active.source_id, artifact_id, basename)
        } else {
            task_artifact_path(
                active.source_id,
                active.job_id,
                active.task_id,
                artifact_id,
                basename,
            )
        }
        .map_err(|error| StoreError::new(error.code()))?;
        let artifact = ArtifactManifestEntry {
            name: request.name.clone(),
            kind: declaration.kind,
            media_type: request.media_type.clone(),
            size_bytes: request.size_bytes,
            sha256: request.sha256.clone(),
            relative_path: final_path.clone(),
        };
        let pending = PendingAttemptUpload {
            artifact_id,
            declaration_name: declaration.name.clone(),
            artifact: artifact.clone(),
        };
        let record = UploadRecord::new(
            upload_id,
            &request.name,
            &final_path,
            request.size_bytes,
            request.sha256.clone(),
            &declaration.name,
            declaration.max_bytes,
        )
        .map_err(|error| StoreError::new(error.code()))?;
        artifacts
            .validate_upload(&record)
            .map_err(|error| StoreError::new(error.code()))?;
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
             final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state, \
             created_at_ms,updated_at_ms) VALUES(?,'attempt',?,?,?,?,?,?,?, ?,0,'receiving',?,?)",
        )
        .bind(upload_id.to_string())
        .bind(attempt_id.to_string())
        .bind(serde_json::to_string(&pending).map_err(|_| corrupt())?)
        .bind(&request.name)
        .bind(artifact_id.to_string())
        .bind(record.staging_relative_path().to_string_lossy().as_ref())
        .bind(&final_path)
        .bind(i64::try_from(request.size_bytes).map_err(|_| invalid())?)
        .bind(request.sha256.as_str())
        .bind(now_ms)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(StartUploadResponse {
            upload_id,
            received_bytes: 0,
            artifact,
        })
    }
}

pub(super) async fn load_upload(
    transaction: &mut Transaction<'_, Sqlite>,
    runner_id: RunnerId,
    upload_id: UploadId,
    now_ms: i64,
) -> Result<ActiveUpload, StoreError> {
    let row = sqlx::query(
        "SELECT u.owner_kind,u.owner_id,u.commit_json,u.name,u.target_id,u.staging_path, \
         u.final_relative_path,u.expected_size_bytes,u.expected_sha256,u.received_bytes,u.state, \
         t.spec_json FROM uploads u JOIN attempts a ON a.id=u.owner_id \
         JOIN tasks t ON t.id=a.task_id WHERE u.id=?",
    )
    .bind(upload_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
    if row.try_get::<String, _>("owner_kind")? != "attempt" {
        return Err(corrupt());
    }
    let attempt_id = AttemptId::from_str(row.try_get("owner_id")?).map_err(|_| corrupt())?;
    let active = active_attempt(transaction, runner_id, attempt_id, now_ms).await?;
    let pending: PendingAttemptUpload =
        serde_json::from_str(row.try_get("commit_json")?).map_err(|_| corrupt())?;
    let (declaration, _) = declaration(&active.spec, &pending.artifact.name)?;
    if pending.declaration_name != declaration.name
        || pending.artifact.kind != declaration.kind
        || pending.artifact_id.to_string() != row.try_get::<String, _>("target_id")?
        || pending.artifact.name != row.try_get::<String, _>("name")?
        || pending.artifact.relative_path != row.try_get::<String, _>("final_relative_path")?
        || pending.artifact.size_bytes != to_u64(row.try_get("expected_size_bytes")?)?
        || pending.artifact.sha256.as_str() != row.try_get::<String, _>("expected_sha256")?
    {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        &pending.artifact.name,
        &pending.artifact.relative_path,
        pending.artifact.size_bytes,
        pending.artifact.sha256.clone(),
        &pending.declaration_name,
        declaration.max_bytes,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(
            to_u64(row.try_get("received_bytes")?)?,
            parse_state(row.try_get("state")?)?,
        )
        .map_err(|_| corrupt())?;
    if record.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok(ActiveUpload { record, pending })
}

async fn active_attempt(
    transaction: &mut Transaction<'_, Sqlite>,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    now_ms: i64,
) -> Result<ActiveAttempt, StoreError> {
    let row = sqlx::query(
        "SELECT a.runner_id,a.state,a.lease_expires_at_ms,t.id AS task_id,t.state AS task_state, \
         t.current_attempt_id,t.spec_json,j.id AS job_id,j.state AS job_state,j.source_id \
         FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id WHERE a.id=?",
    )
    .bind(attempt_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
    if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
        != Some(runner_id.to_string().as_str())
        || row.try_get::<String, _>("state")? != "leased"
        || row.try_get::<String, _>("task_state")? != "leased"
        || row.try_get::<String, _>("job_state")? != "running"
        || row
            .try_get::<Option<String>, _>("current_attempt_id")?
            .as_deref()
            != Some(attempt_id.to_string().as_str())
    {
        return Err(StoreError::new(ErrorCode::StaleAttempt));
    }
    if row.try_get::<i64, _>("lease_expires_at_ms")? <= now_ms {
        return Err(StoreError::new(ErrorCode::LeaseExpired));
    }
    Ok(ActiveAttempt {
        source_id: row
            .try_get::<String, _>("source_id")?
            .parse()
            .map_err(|_| corrupt())?,
        job_id: row
            .try_get::<String, _>("job_id")?
            .parse()
            .map_err(|_| corrupt())?,
        task_id: row
            .try_get::<String, _>("task_id")?
            .parse()
            .map_err(|_| corrupt())?,
        spec: serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?,
    })
}

fn declaration<'a, 'b>(
    spec: &'a CompiledTaskSpec,
    name: &'b str,
) -> Result<(&'a ArtifactDeclaration, &'b str), StoreError> {
    for declaration in &spec.artifacts {
        if name == declaration.name && declaration.max_files.is_none() {
            return Ok((declaration, name));
        }
        if let Some(basename) = name
            .strip_prefix(&declaration.name)
            .and_then(|suffix| suffix.strip_prefix('/'))
            && declaration.max_files.is_some()
            && safe_basename(basename)
        {
            return Ok((declaration, basename));
        }
    }
    Err(StoreError::new(ErrorCode::ArtifactUndeclared))
}

fn safe_basename(value: &str) -> bool {
    !value.is_empty() && !value.starts_with('.') && !value.contains(['/', '\\', '\0'])
}

pub(super) fn retention(kind: ArtifactKind) -> &'static str {
    match kind {
        ArtifactKind::SourceOriginal | ArtifactKind::Subtitle | ArtifactKind::Danmaku => "source",
        ArtifactKind::TaskLog | ArtifactKind::AiAudit => "failed_audit",
        _ => "published",
    }
}

fn parse_state(value: &str) -> Result<UploadState, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}
fn to_u64(value: i64) -> Result<u64, StoreError> {
    value.try_into().map_err(|_| corrupt())
}
fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
