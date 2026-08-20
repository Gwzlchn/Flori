use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ArtifactWhen, AttemptAck, AttemptId, AttemptState,
    CompiledTaskSpec, ErrorCode, FailAttemptRequest, RunnerId, UploadId, UploadState,
};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::{
    super::{Store, StoreError},
    poll::server_log,
    terminal_common::{
        cleanup_failed_uploads, commit_uploads, exact_moved, load_attempt_uploads, manifest_digest,
        required_present,
    },
    upload::active_attempt,
    upload_rule::{declaration, retention},
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
        let state = attempt_state(&mut transaction, runner_id, attempt_id).await?;
        if state == "failed" {
            replay_failure(&mut transaction, artifacts, runner_id, attempt_id, request).await?;
            transaction.rollback().await?;
            cleanup_failed_uploads(&self.pool, artifacts, runner_id, attempt_id).await?;
            return Ok(failed_ack(attempt_id));
        }
        if state != "leased" {
            return Err(StoreError::new(ErrorCode::StaleAttempt));
        }
        let active = active_attempt(&mut transaction, runner_id, attempt_id, now_ms).await?;
        server_log::finalize(
            &mut transaction,
            artifacts,
            &active,
            attempt_id,
            true,
            now_ms,
        )
        .await?;
        let uploads = load_attempt_uploads(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let mut committed = Vec::new();
        for upload in &uploads {
            let (declared, _) = declaration(&active.spec, &upload.pending.artifact.name)?;
            if declared.when == ArtifactWhen::Always && upload.record.state() == UploadState::Moved
            {
                committed.push(upload);
            }
        }
        exact_moved(artifacts, &committed)?;
        required_present(&active, &committed, true)?;
        verify_manifest(
            active.job_id,
            active.task_id,
            attempt_id,
            committed
                .iter()
                .map(|upload| upload.pending.artifact.clone())
                .collect(),
            request.manifest_sha256.as_ref(),
        )?;
        commit_uploads(&mut transaction, &active, attempt_id, &committed, now_ms).await?;
        for upload in committed {
            sqlx::query("DELETE FROM uploads WHERE owner_kind='attempt' AND owner_id=? AND name=?")
                .bind(attempt_id.to_string())
                .bind(&upload.pending.artifact.name)
                .execute(&mut *transaction)
                .await?;
        }
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
        cleanup_failed_uploads(&self.pool, artifacts, runner_id, attempt_id).await?;
        Ok(failed_ack(attempt_id))
    }
}

async fn attempt_state(
    transaction: &mut Transaction<'_, Sqlite>,
    runner_id: RunnerId,
    attempt_id: AttemptId,
) -> Result<String, StoreError> {
    let row = sqlx::query("SELECT runner_id,state FROM attempts WHERE id=?")
        .bind(attempt_id.to_string())
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::StaleAttempt))?;
    if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
        != Some(runner_id.to_string().as_str())
    {
        return Err(StoreError::new(ErrorCode::StaleAttempt));
    }
    row.try_get("state").map_err(Into::into)
}

async fn replay_failure(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    request: &FailAttemptRequest,
) -> Result<(), StoreError> {
    let row = sqlx::query(
        "SELECT a.runner_id,a.error_code,t.spec_json,t.id AS task_id,j.id AS job_id,j.source_id \
         FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id WHERE a.id=?",
    )
    .bind(attempt_id.to_string())
    .fetch_one(&mut **transaction)
    .await?;
    if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
        != Some(runner_id.to_string().as_str())
        || row.try_get::<Option<String>, _>("error_code")?.as_deref()
            != Some(wire_error(request.error_code).as_str())
    {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    let job_id: flori_core::JobId = row
        .try_get::<String, _>("job_id")?
        .parse()
        .map_err(|_| corrupt())?;
    let task_id: flori_core::TaskId = row
        .try_get::<String, _>("task_id")?
        .parse()
        .map_err(|_| corrupt())?;
    let row_source_id: String = row.try_get("source_id")?;
    let spec: CompiledTaskSpec =
        serde_json::from_str(row.try_get("spec_json")?).map_err(|_| corrupt())?;
    let rows = sqlx::query(
        "SELECT source_id,job_id,task_id,name,kind,media_type,size_bytes,sha256,relative_path, \
         retention,origin \
         FROM artifacts WHERE attempt_id=? ORDER BY name",
    )
    .bind(attempt_id.to_string())
    .fetch_all(&mut **transaction)
    .await?;
    let mut entries = Vec::with_capacity(rows.len());
    for row in rows {
        let name: String = row.try_get("name")?;
        let (declared, _) = declaration(&spec, &name)?;
        let kind = parse_kind(row.try_get("kind")?)?;
        let entry = ArtifactManifestEntry {
            name,
            kind,
            media_type: row.try_get("media_type")?,
            size_bytes: row
                .try_get::<i64, _>("size_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            sha256: flori_core::Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
                .map_err(|_| corrupt())?,
            relative_path: row.try_get("relative_path")?,
        };
        if row.try_get::<String, _>("origin")? != "produced"
            || row.try_get::<String, _>("source_id")? != row_source_id
            || row.try_get::<String, _>("job_id")? != job_id.to_string()
            || row.try_get::<String, _>("task_id")? != task_id.to_string()
            || declared.when != ArtifactWhen::Always
            || declared.kind != kind
            || !kind.accepts_media_type(&entry.media_type)
            || row.try_get::<String, _>("retention")? != retention(kind)
        {
            return Err(corrupt());
        }
        let mut record = UploadRecord::new(
            UploadId::generate(),
            &entry.name,
            &entry.relative_path,
            entry.size_bytes,
            entry.sha256.clone(),
            &declared.name,
            declared.max_bytes,
        )
        .map_err(|_| corrupt())?;
        record
            .restore_progress(entry.size_bytes, UploadState::Moved)
            .map_err(|_| corrupt())?;
        if artifacts
            .recovery_action(&record, true)
            .map_err(|error| StoreError::new(error.code()))?
            != RecoveryAction::RetryCommit
        {
            return Err(corrupt());
        }
        entries.push(entry);
    }
    for declared in spec.artifacts.iter().filter(|item| {
        item.required && item.when == ArtifactWhen::Always && item.kind != ArtifactKind::TaskLog
    }) {
        if !entries.iter().any(|entry| entry.name == declared.name) {
            return Err(StoreError::new(ErrorCode::ArtifactUndeclared));
        }
    }
    verify_manifest(
        job_id,
        task_id,
        attempt_id,
        entries,
        request.manifest_sha256.as_ref(),
    )?;
    Ok(())
}

fn verify_manifest(
    job_id: flori_core::JobId,
    task_id: flori_core::TaskId,
    attempt_id: AttemptId,
    entries: Vec<ArtifactManifestEntry>,
    expected: Option<&flori_core::Sha256Digest>,
) -> Result<(), StoreError> {
    let runner_entries = entries
        .into_iter()
        .filter(|entry| entry.kind != ArtifactKind::TaskLog)
        .collect::<Vec<_>>();
    match (runner_entries.is_empty(), expected) {
        (true, None) => Ok(()),
        (false, Some(expected))
            if manifest_digest(&flori_core::ArtifactManifest::new(
                job_id,
                task_id,
                attempt_id,
                runner_entries,
            ))? == *expected =>
        {
            Ok(())
        }
        _ => Err(StoreError::new(ErrorCode::DigestMismatch)),
    }
}

fn parse_kind(value: &str) -> Result<ArtifactKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}
fn wire_error(value: ErrorCode) -> String {
    serde_json::to_string(&value)
        .expect("closed error enum")
        .trim_matches('"')
        .to_owned()
}
fn failed_ack(exec_id: AttemptId) -> AttemptAck {
    AttemptAck {
        exec_id,
        state: AttemptState::Failed,
    }
}
fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
