use std::fmt::Write;

use flori_core::{ArtifactKind, ArtifactRetention, ErrorCode, PendingMaterializeCommit, TaskState};
use sha2::{Digest, Sha256};
use sqlx::{Sqlite, Transaction};

use super::{super::StoreError, attempt::promote_ready, wire::executor};

pub(super) async fn commit_plan(
    transaction: &mut Transaction<'_, Sqlite>,
    pending: &PendingMaterializeCommit,
    request_key: &str,
    request_sha256: &str,
) -> Result<(), StoreError> {
    let prompt_json = serde_json::to_string(&pending.prompt_snapshot).map_err(|_| corrupt())?;
    let inputs_json = serde_json::to_string(&pending.inputs).map_err(|_| corrupt())?;
    sqlx::query(
        "INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,rerun_of_job_id, \
         rerun_from_task_key,state,prompt_snapshot_id,prompt_snapshot_sha256, \
         prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms) \
         VALUES(?,?,?,'task_rerun',?,?,'queued',?,?,?,?,?,?,?)",
    )
    .bind(pending.job_id.to_string())
    .bind(pending.source_id.to_string())
    .bind(pending.pipeline_revision_id.to_string())
    .bind(pending.base_job_id.to_string())
    .bind(&pending.from_task_key)
    .bind(pending.prompt_snapshot_id.to_string())
    .bind(digest(&prompt_json))
    .bind(prompt_json)
    .bind(inputs_json)
    .bind(request_key)
    .bind(request_sha256)
    .bind(pending.created_at_ms)
    .execute(&mut **transaction)
    .await?;
    for task in &pending.tasks {
        if !matches!(task.state, TaskState::Pending | TaskState::Skipped)
            || task.ai_selection.is_some() && task.state != TaskState::Pending
        {
            return Err(corrupt());
        }
        let selection = task.ai_selection.as_ref();
        sqlx::query(
            "INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state, \
             pinned_runner_id,selected_model,selected_effort,runner_config_revision, \
             attempt_limit,timeout_ms,finished_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .bind(task.task_id.to_string())
        .bind(pending.job_id.to_string())
        .bind(&task.task_key)
        .bind(executor(task.spec.executor))
        .bind(serde_json::to_string(&task.spec).map_err(|_| corrupt())?)
        .bind(serde_json::to_string(&task.bindings).map_err(|_| corrupt())?)
        .bind(task_state(task.state))
        .bind(selection.map(|item| item.runner_id.to_string()))
        .bind(selection.map(|item| item.model.as_str()))
        .bind(selection.map(|item| item.effort.as_str()))
        .bind(
            selection
                .map(|item| i64::try_from(item.runner_config_revision))
                .transpose()
                .map_err(|_| corrupt())?,
        )
        .bind(i64::from(task.spec.retry) + 1)
        .bind(i64::try_from(task.spec.timeout_ms).map_err(|_| corrupt())?)
        .bind((task.state == TaskState::Skipped).then_some(pending.created_at_ms))
        .execute(&mut **transaction)
        .await?;
    }
    for artifact in &pending.artifacts {
        sqlx::query(
            "INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin, \
             materialized_from_artifact_id,name,kind,media_type,file_name,size_bytes,sha256, \
             relative_path,retention,created_at_ms) \
             VALUES(?,?,?,?,NULL,'materialized',?,?,?,?,?,?,?,?,?,?)",
        )
        .bind(artifact.artifact_id.to_string())
        .bind(pending.source_id.to_string())
        .bind(pending.job_id.to_string())
        .bind(artifact.task_id.to_string())
        .bind(artifact.source_artifact_id.to_string())
        .bind(&artifact.name)
        .bind(artifact_kind(artifact.kind))
        .bind(&artifact.media_type)
        .bind(&artifact.file_name)
        .bind(i64::try_from(artifact.size_bytes).map_err(|_| corrupt())?)
        .bind(artifact.sha256.as_str())
        .bind(&artifact.final_relative_path)
        .bind(retention(artifact.retention))
        .bind(pending.created_at_ms)
        .execute(&mut **transaction)
        .await?;
    }
    sqlx::query("DELETE FROM uploads WHERE owner_kind='materialize' AND owner_id=?")
        .bind(pending.job_id.to_string())
        .execute(&mut **transaction)
        .await?;
    promote_ready(
        transaction,
        &pending.job_id.to_string(),
        pending.created_at_ms,
    )
    .await
}

fn digest(value: &str) -> String {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    output
}

fn task_state(state: TaskState) -> &'static str {
    match state {
        TaskState::Pending => "pending",
        TaskState::Skipped => "skipped",
        _ => unreachable!("validated materialize task state"),
    }
}

fn artifact_kind(kind: ArtifactKind) -> String {
    serde_json::to_string(&kind)
        .expect("closed ArtifactKind serializes")
        .trim_matches('"')
        .to_owned()
}

fn retention(value: ArtifactRetention) -> String {
    serde_json::to_string(&value)
        .expect("closed ArtifactRetention serializes")
        .trim_matches('"')
        .to_owned()
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
