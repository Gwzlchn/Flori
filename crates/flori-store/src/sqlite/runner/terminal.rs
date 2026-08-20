use std::str::FromStr;

use flori_core::{
    ArtifactId, ArtifactKind, ArtifactManifestEntry, AttemptAck, AttemptId, AttemptState,
    CompiledTaskSpec, CompleteAttemptRequest, ErrorCode, JobId, RunnerId, SourceId, TaskId,
    UploadId, UploadState,
};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use super::{
    super::{Store, StoreError},
    poll::server_log,
    terminal_common::{
        commit_uploads, exact_moved, load_attempt_uploads, manifest, manifest_digest,
        required_present,
    },
    upload::active_attempt,
    upload_rule::{declaration, retention},
};

impl Store {
    pub async fn complete_authenticated_attempt(
        &self,
        artifacts: &NasArtifactStore,
        runner_id: RunnerId,
        attempt_id: AttemptId,
        request: &CompleteAttemptRequest,
        now_ms: i64,
    ) -> Result<AttemptAck, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let state = attempt_state(&mut transaction, runner_id, attempt_id).await?;
        if state == "succeeded" {
            replay_complete(
                &mut transaction,
                artifacts,
                runner_id,
                attempt_id,
                &request.manifest_sha256,
            )
            .await?;
            transaction.rollback().await?;
            return Ok(AttemptAck {
                exec_id: attempt_id,
                state: AttemptState::Succeeded,
            });
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
            false,
            now_ms,
        )
        .await?;
        let uploads = load_attempt_uploads(&mut transaction, runner_id, attempt_id, now_ms).await?;
        let selected = uploads.iter().collect::<Vec<_>>();
        exact_moved(artifacts, &selected)?;
        required_present(&active, &selected, false)?;
        let open_usage: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM ai_usage WHERE attempt_id=? AND state='started'",
        )
        .bind(attempt_id.to_string())
        .fetch_one(&mut *transaction)
        .await?;
        if open_usage != 0 {
            return Err(StoreError::new(ErrorCode::UsageConflict));
        }
        let runner_owned = selected
            .iter()
            .copied()
            .filter(|upload| upload.pending.artifact.kind != ArtifactKind::TaskLog)
            .collect::<Vec<_>>();
        if manifest_digest(&manifest(&active, attempt_id, &runner_owned))?
            != request.manifest_sha256
        {
            return Err(StoreError::new(ErrorCode::DigestMismatch));
        }
        commit_uploads(&mut transaction, &active, attempt_id, &selected, now_ms).await?;
        sqlx::query("DELETE FROM uploads WHERE owner_kind='attempt' AND owner_id=?")
            .bind(attempt_id.to_string())
            .execute(&mut *transaction)
            .await?;
        super::super::scheduler::finish_success(
            &mut transaction,
            &attempt_id.to_string(),
            &active.task_id.to_string(),
            &active.job_id.to_string(),
            now_ms,
        )
        .await?;
        transaction.commit().await?;
        Ok(AttemptAck {
            exec_id: attempt_id,
            state: AttemptState::Succeeded,
        })
    }
}

async fn attempt_state(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
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
    row.try_get("state").map_err(StoreError::from)
}

#[allow(clippy::too_many_lines)]
async fn replay_complete(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    artifacts: &NasArtifactStore,
    runner_id: RunnerId,
    attempt_id: AttemptId,
    expected: &flori_core::Sha256Digest,
) -> Result<(), StoreError> {
    let row = sqlx::query(
        "SELECT a.runner_id,t.state AS task_state,t.spec_json,t.id AS task_id,j.id AS job_id, \
         j.source_id,(SELECT count(*) FROM uploads u WHERE u.owner_kind='attempt' \
         AND u.owner_id=a.id) AS upload_count,(SELECT count(*) FROM ai_usage u \
         WHERE u.attempt_id=a.id AND u.state='started') AS open_usage \
         FROM attempts a JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id WHERE a.id=?",
    )
    .bind(attempt_id.to_string())
    .fetch_one(&mut **transaction)
    .await?;
    if row.try_get::<Option<String>, _>("runner_id")?.as_deref()
        != Some(runner_id.to_string().as_str())
        || row.try_get::<String, _>("task_state")? != "succeeded"
        || row.try_get::<i64, _>("upload_count")? != 0
        || row.try_get::<i64, _>("open_usage")? != 0
    {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    let source_id = SourceId::from_str(row.try_get("source_id")?).map_err(|_| corrupt())?;
    let job_id = JobId::from_str(row.try_get("job_id")?).map_err(|_| corrupt())?;
    let task_id = TaskId::from_str(row.try_get("task_id")?).map_err(|_| corrupt())?;
    let spec = serde_json::from_str::<CompiledTaskSpec>(row.try_get("spec_json")?)
        .map_err(|_| corrupt())?;
    let rows = sqlx::query(
        "SELECT id,source_id,job_id,task_id,name,kind,media_type,size_bytes,sha256,relative_path, \
         retention,origin \
         FROM artifacts WHERE attempt_id=? ORDER BY name",
    )
    .bind(attempt_id.to_string())
    .fetch_all(&mut **transaction)
    .await?;
    let mut entries = Vec::with_capacity(rows.len());
    for row in rows {
        ArtifactId::from_str(row.try_get("id")?).map_err(|_| corrupt())?;
        if row.try_get::<String, _>("origin")? != "produced"
            || row.try_get::<String, _>("source_id")? != source_id.to_string()
            || row.try_get::<String, _>("job_id")? != job_id.to_string()
            || row.try_get::<String, _>("task_id")? != task_id.to_string()
        {
            return Err(corrupt());
        }
        let name: String = row.try_get("name")?;
        let kind = parse_kind(row.try_get("kind")?)?;
        let (declared, _) = declaration(&spec, &name)?;
        let media_type: String = row.try_get("media_type")?;
        if declared.kind != kind
            || !kind.accepts_media_type(&media_type)
            || row.try_get::<String, _>("retention")? != retention(kind)
        {
            return Err(corrupt());
        }
        let entry = ArtifactManifestEntry {
            name,
            kind,
            media_type,
            size_bytes: row
                .try_get::<i64, _>("size_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            sha256: flori_core::Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
                .map_err(|_| corrupt())?,
            relative_path: row.try_get("relative_path")?,
        };
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
    for declaration in spec.artifacts.iter().filter(|item| item.required) {
        if !entries.iter().any(|entry| {
            entry.name == declaration.name
                || entry.name.starts_with(&format!("{}/", declaration.name))
        }) {
            return Err(StoreError::new(ErrorCode::ArtifactUndeclared));
        }
    }
    let runner_entries = entries
        .into_iter()
        .filter(|entry| entry.kind != ArtifactKind::TaskLog)
        .collect();
    let actual = manifest_digest(&flori_core::ArtifactManifest::new(
        job_id,
        task_id,
        attempt_id,
        runner_entries,
    ))?;
    if &actual != expected {
        return Err(StoreError::new(ErrorCode::DigestMismatch));
    }
    Ok(())
}

fn parse_kind(value: &str) -> Result<ArtifactKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
