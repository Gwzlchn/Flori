use std::{fmt::Write, str::FromStr};

use flori_core::{
    ErrorCode, JobId, PendingMaterializeCommit, RerunJobRequest, RerunMode, Sha256Digest,
};
use flori_pipeline::Compilation;
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    rerun_commit::commit_plan,
    rerun_copy::{copy_all, verify_ready},
    rerun_plan::build_plan,
};

impl Store {
    pub async fn rerun_from_task(
        &self,
        artifacts: &NasArtifactStore,
        base_job_id: JobId,
        request: &RerunJobRequest,
        compilation: &Compilation,
        now_ms: i64,
    ) -> Result<JobId, StoreError> {
        if request.request_key.is_empty()
            || request.mode != RerunMode::FromTask
            || request.from_task_key.as_deref().is_none_or(str::is_empty)
            || now_ms < 0
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let request_sha = intent_digest(base_job_id, request, compilation)?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        if let Some(job_id) = existing_job(
            &mut transaction,
            base_job_id,
            request,
            compilation,
            &request_sha,
        )
        .await?
        {
            transaction.rollback().await?;
            return Ok(job_id);
        }
        let pending = if let Some(existing) =
            existing_pending(&mut transaction, &request.request_key, &request_sha).await?
        {
            let validated = build_plan(
                &mut transaction,
                base_job_id,
                request,
                compilation,
                existing.created_at_ms,
                Some(&existing),
            )
            .await?;
            transaction.rollback().await?;
            validated
        } else {
            let planned = build_plan(
                &mut transaction,
                base_job_id,
                request,
                compilation,
                now_ms,
                None,
            )
            .await?;
            reject_busy(&mut transaction, &planned).await?;
            if planned.artifacts.is_empty() {
                commit_plan(
                    &mut transaction,
                    &planned,
                    &request.request_key,
                    &request_sha,
                )
                .await?;
                transaction.commit().await?;
                return Ok(planned.job_id);
            }
            insert_ledgers(
                &mut transaction,
                &planned,
                &request.request_key,
                &request_sha,
            )
            .await?;
            transaction.commit().await?;
            planned
        };

        copy_all(
            self,
            artifacts,
            &pending,
            &request.request_key,
            &request_sha,
        )
        .await?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let persisted = existing_pending(&mut transaction, &request.request_key, &request_sha)
            .await?
            .ok_or_else(corrupt)?;
        let validated = build_plan(
            &mut transaction,
            base_job_id,
            request,
            compilation,
            persisted.created_at_ms,
            Some(&persisted),
        )
        .await?;
        reject_active_job(&mut transaction, &validated).await?;
        verify_ready(
            &mut transaction,
            artifacts,
            &validated,
            &request.request_key,
            &request_sha,
        )
        .await?;
        commit_plan(
            &mut transaction,
            &validated,
            &request.request_key,
            &request_sha,
        )
        .await?;
        transaction.commit().await?;
        Ok(validated.job_id)
    }
}

async fn insert_ledgers(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
    request_key: &str,
    request_sha256: &str,
) -> Result<(), StoreError> {
    let commit_json = serde_json::to_string(pending).map_err(|_| corrupt())?;
    for (index, artifact) in pending.artifacts.iter().enumerate() {
        let task_key = pending
            .tasks
            .iter()
            .find(|task| task.task_id == artifact.task_id)
            .map(|task| task.task_key.as_str())
            .ok_or_else(corrupt)?;
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,request_key,request_sha256,commit_json, \
             name,target_id,source_artifact_id,staging_path,final_relative_path, \
             expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) \
             VALUES(?,'materialize',?,?,?,?,?,?,?,?,?,?,?,0,'receiving',?,?)",
        )
        .bind(artifact.upload_id.to_string())
        .bind(pending.job_id.to_string())
        .bind((index == 0).then_some(request_key))
        .bind(request_sha256)
        .bind(&commit_json)
        .bind(format!("{task_key}/{}", artifact.name))
        .bind(artifact.artifact_id.to_string())
        .bind(artifact.source_artifact_id.to_string())
        .bind(format!(".staging/uploads/{}", artifact.upload_id))
        .bind(&artifact.final_relative_path)
        .bind(
            i64::try_from(artifact.size_bytes)
                .map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?,
        )
        .bind(artifact.sha256.as_str())
        .bind(pending.created_at_ms)
        .bind(pending.created_at_ms)
        .execute(&mut **transaction)
        .await?;
    }
    Ok(())
}

async fn existing_job(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    base_job_id: JobId,
    request: &RerunJobRequest,
    compilation: &Compilation,
    request_sha: &str,
) -> Result<Option<JobId>, StoreError> {
    let Some(row) = sqlx::query(
        "SELECT j.id,j.trigger,j.rerun_of_job_id,j.rerun_from_task_key,j.request_sha256, \
         r.yaml_sha256 FROM jobs j JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id \
         WHERE j.request_key=?",
    )
    .bind(&request.request_key)
    .fetch_optional(&mut **transaction)
    .await?
    else {
        return Ok(None);
    };
    if row.try_get::<String, _>("trigger")? != "task_rerun"
        || row
            .try_get::<Option<String>, _>("rerun_of_job_id")?
            .as_deref()
            != Some(base_job_id.to_string().as_str())
        || row
            .try_get::<Option<String>, _>("rerun_from_task_key")?
            .as_deref()
            != request.from_task_key.as_deref()
        || row.try_get::<String, _>("request_sha256")? != request_sha
        || row.try_get::<String, _>("yaml_sha256")? != compilation.sha256
    {
        return Err(StoreError::new(ErrorCode::IdempotencyConflict));
    }
    Ok(Some(
        JobId::from_str(row.try_get("id")?).map_err(|_| corrupt())?,
    ))
}

async fn existing_pending(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    request_key: &str,
    request_sha: &str,
) -> Result<Option<PendingMaterializeCommit>, StoreError> {
    let Some(row) = sqlx::query(
        "SELECT request_sha256,commit_json FROM uploads \
         WHERE owner_kind='materialize' AND request_key=?",
    )
    .bind(request_key)
    .fetch_optional(&mut **transaction)
    .await?
    else {
        return Ok(None);
    };
    if row
        .try_get::<Option<String>, _>("request_sha256")?
        .as_deref()
        != Some(request_sha)
    {
        return Err(StoreError::new(ErrorCode::IdempotencyConflict));
    }
    serde_json::from_str(row.try_get("commit_json")?)
        .map(Some)
        .map_err(|_| corrupt())
}

async fn reject_busy(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    reject_active_job(transaction, pending).await?;
    let commits: Vec<String> = sqlx::query_scalar(
        "SELECT DISTINCT commit_json FROM uploads WHERE owner_kind='materialize'",
    )
    .fetch_all(&mut **transaction)
    .await?;
    for json in commits {
        let other: PendingMaterializeCommit = serde_json::from_str(&json).map_err(|_| corrupt())?;
        if other.source_id == pending.source_id {
            return Err(StoreError::new(ErrorCode::SourceBusy));
        }
    }
    Ok(())
}

async fn reject_active_job(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    let active: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM jobs WHERE source_id=? AND state IN ('queued','running')",
    )
    .bind(pending.source_id.to_string())
    .fetch_one(&mut **transaction)
    .await?;
    if active != 0 {
        return Err(StoreError::new(ErrorCode::SourceBusy));
    }
    Ok(())
}

pub(super) fn intent_digest(
    base_job_id: JobId,
    request: &RerunJobRequest,
    compilation: &Compilation,
) -> Result<String, StoreError> {
    let request_json =
        serde_json::to_string(request).map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?;
    let mut hasher = Sha256::new();
    for value in [
        base_job_id.to_string().as_bytes(),
        request_json.as_bytes(),
        compilation.sha256.as_bytes(),
    ] {
        hasher.update((value.len() as u64).to_be_bytes());
        hasher.update(value);
    }
    let mut output = String::with_capacity(64);
    for byte in hasher.finalize() {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(output.clone()).expect("SHA-256 formatter is canonical");
    Ok(output)
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
