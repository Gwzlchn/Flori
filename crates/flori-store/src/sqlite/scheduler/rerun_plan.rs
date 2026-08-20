use std::{collections::BTreeMap, collections::BTreeSet, str::FromStr};

use flori_core::{
    ErrorCode, Executor, JobId, JobInputs, PendingMaterializeCommit, PendingTaskCommit,
    PipelineRevisionId, PromptSnapshot, PromptSnapshotId, RerunJobRequest, RerunMode, SourceId,
    SourceKind, TaskId, TaskState,
};
use flori_pipeline::{Compilation, compile};
use sqlx::{Row, Sqlite, Transaction};

use super::{
    super::StoreError,
    job::freeze_tasks,
    pipeline::valid_compilation,
    rerun_ai::validate_ai_selection,
    rerun_artifact::plan_artifacts,
    snapshot::{current_prompt_snapshot, freeze_prompt_snapshot},
    wire::executor,
};

pub(super) async fn build_plan(
    transaction: &mut Transaction<'_, Sqlite>,
    base_job_id: JobId,
    request: &RerunJobRequest,
    compilation: &Compilation,
    now_ms: i64,
    reuse: Option<&PendingMaterializeCommit>,
) -> Result<PendingMaterializeCommit, StoreError> {
    let from_key = request
        .from_task_key
        .as_deref()
        .filter(|_| request.mode == RerunMode::FromTask)
        .ok_or_else(|| StoreError::new(ErrorCode::RerunBoundaryInvalid))?;
    let row = sqlx::query(
        "SELECT j.source_id,j.pipeline_revision_id,j.state,j.inputs_json,s.kind,s.domain_id, \
         s.current_job_id,p.current_revision_id,p.key,r.yaml_sha256,r.yaml_text \
         FROM jobs j JOIN sources s ON s.id=j.source_id \
         JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id \
         JOIN pipelines p ON p.id=r.pipeline_id WHERE j.id=?",
    )
    .bind(base_job_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
    if row.try_get::<String, _>("state")? != "succeeded"
        || row
            .try_get::<Option<String>, _>("current_job_id")?
            .as_deref()
            != Some(base_job_id.to_string().as_str())
    {
        return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
    }
    let revision_id = PipelineRevisionId::from_str(row.try_get("pipeline_revision_id")?)
        .map_err(|_| corrupt())?;
    if reuse.is_none()
        && row
            .try_get::<Option<String>, _>("current_revision_id")?
            .as_deref()
            != Some(revision_id.to_string().as_str())
        || row.try_get::<String, _>("yaml_sha256")? != compilation.sha256
        || !valid_compilation(compilation)
    {
        return Err(StoreError::new(ErrorCode::PipelineInvalid));
    }
    let stored = compile(
        row.try_get("key")?,
        row.try_get::<String, _>("yaml_text")?.as_bytes(),
    )
    .map_err(|_| StoreError::new(ErrorCode::PipelineInvalid))?;
    if stored != *compilation {
        return Err(StoreError::new(ErrorCode::PipelineInvalid));
    }
    let source_id = SourceId::from_str(row.try_get("source_id")?).map_err(|_| corrupt())?;
    let source_kind = parse_source_kind(row.try_get("kind")?)?;
    let inputs_json: String = row.try_get("inputs_json")?;
    let mut inputs: JobInputs = serde_json::from_str(&inputs_json).map_err(|_| corrupt())?;
    if serde_json::to_string(&inputs).map_err(|_| corrupt())? != inputs_json {
        return Err(corrupt());
    }
    if compilation
        .pipeline
        .tasks
        .get(from_key)
        .is_some_and(|task| task.executor == Executor::AiDocumentTranslate)
    {
        inputs.translate = true;
    }
    let frozen = freeze_tasks(compilation, source_kind, inputs.translate)?;
    let target = frozen
        .iter()
        .find(|task| task.key == from_key && task.included)
        .ok_or_else(|| StoreError::new(ErrorCode::RerunBoundaryInvalid))?;
    let rerun = successors(&frozen, &target.key);
    if reuse.is_none() {
        validate_ai_selection(transaction, request.ai_selection.as_ref(), &rerun, &frozen).await?;
    }
    let required_prompts = frozen
        .iter()
        .filter(|task| task.included)
        .filter_map(|task| task.prompt_key.as_deref())
        .collect::<BTreeSet<_>>();
    let domain_id = row
        .try_get::<String, _>("domain_id")?
        .parse()
        .map_err(|_| corrupt())?;
    let prompt_snapshot: PromptSnapshot = match reuse {
        Some(pending) => pending.prompt_snapshot.clone(),
        None => current_prompt_snapshot(transaction, domain_id, &required_prompts).await?,
    };
    freeze_prompt_snapshot(&prompt_snapshot, domain_id, &required_prompts)
        .map_err(|_| corrupt())?;
    let base_tasks = validate_base_tasks(transaction, base_job_id, &frozen).await?;
    let job_id = reuse.map_or_else(JobId::generate, |pending| pending.job_id);
    let tasks = frozen
        .iter()
        .map(|task| PendingTaskCommit {
            task_id: reuse
                .and_then(|pending| {
                    pending
                        .tasks
                        .iter()
                        .find(|existing| existing.task_key == task.key)
                })
                .map_or_else(TaskId::generate, |existing| existing.task_id),
            task_key: task.key.clone(),
            spec: task.spec.clone(),
            bindings: serde_json::from_str(&task.bindings_json)
                .expect("freeze_tasks produced strict bindings"),
            state: if task.included && rerun.contains(&task.key) {
                TaskState::Pending
            } else {
                TaskState::Skipped
            },
            ai_selection: request
                .ai_selection
                .as_ref()
                .filter(|selection| selection.task_key == task.key)
                .cloned(),
        })
        .collect::<Vec<_>>();
    let materialize_keys = frozen
        .iter()
        .filter(|task| task.included && !rerun.contains(&task.key))
        .map(|task| task.key.clone())
        .collect::<Vec<_>>();
    let artifacts = plan_artifacts(
        transaction,
        source_id,
        base_job_id,
        job_id,
        &base_tasks,
        &tasks,
        &materialize_keys,
        reuse.map(|pending| pending.artifacts.as_slice()),
    )
    .await?;
    let pending = PendingMaterializeCommit {
        source_id,
        base_job_id,
        job_id,
        pipeline_revision_id: revision_id,
        prompt_snapshot_id: reuse.map_or_else(PromptSnapshotId::generate, |pending| {
            pending.prompt_snapshot_id
        }),
        prompt_snapshot,
        inputs,
        from_task_key: from_key.to_owned(),
        created_at_ms: reuse.map_or(now_ms, |pending| pending.created_at_ms),
        tasks,
        artifacts,
    };
    if reuse.is_some_and(|existing| *existing != pending) {
        return Err(corrupt());
    }
    Ok(pending)
}

async fn validate_base_tasks(
    transaction: &mut Transaction<'_, Sqlite>,
    base_job_id: JobId,
    frozen: &[super::job::FrozenTask],
) -> Result<BTreeMap<String, (TaskId, TaskState, Option<String>)>, StoreError> {
    let rows = sqlx::query(
        "SELECT t.id,t.task_key,t.executor,t.spec_json,t.input_bindings_json,t.state, \
         t.attempt_limit,t.timeout_ms,t.current_attempt_id,a.task_id AS attempt_task_id, \
         a.state AS attempt_state FROM tasks t LEFT JOIN attempts a ON a.id=t.current_attempt_id \
         WHERE t.job_id=? ORDER BY t.task_key",
    )
    .bind(base_job_id.to_string())
    .fetch_all(&mut **transaction)
    .await?;
    if rows.len() != frozen.len() {
        return Err(corrupt());
    }
    let mut base = BTreeMap::new();
    for row in rows {
        let id: String = row.try_get("id")?;
        let key: String = row.try_get("task_key")?;
        let task = frozen
            .iter()
            .find(|task| task.key == key)
            .ok_or_else(corrupt)?;
        if row.try_get::<String, _>("executor")? != executor(task.spec.executor)
            || row.try_get::<String, _>("spec_json")? != task.spec_json
            || row.try_get::<String, _>("input_bindings_json")? != task.bindings_json
            || row.try_get::<i64, _>("attempt_limit")? != i64::from(task.spec.retry) + 1
            || row.try_get::<i64, _>("timeout_ms")?
                != i64::try_from(task.spec.timeout_ms).map_err(|_| corrupt())?
        {
            return Err(corrupt());
        }
        let state = parse_task_state(row.try_get("state")?)?;
        let current_attempt: Option<String> = row.try_get("current_attempt_id")?;
        let attempt_task: Option<String> = row.try_get("attempt_task_id")?;
        let attempt_state: Option<String> = row.try_get("attempt_state")?;
        let terminal_is_valid = match state {
            TaskState::Succeeded => {
                current_attempt.is_some()
                    && attempt_task.as_deref() == Some(id.as_str())
                    && attempt_state.as_deref() == Some("succeeded")
            }
            TaskState::Skipped => {
                current_attempt.is_none() && attempt_task.is_none() && attempt_state.is_none()
            }
            _ => false,
        };
        if !terminal_is_valid {
            return Err(corrupt());
        }
        base.insert(
            key,
            (
                TaskId::from_str(&id).map_err(|_| corrupt())?,
                state,
                current_attempt,
            ),
        );
    }
    Ok(base)
}

fn successors(tasks: &[super::job::FrozenTask], target: &str) -> BTreeSet<String> {
    let mut rerun = BTreeSet::from([target.to_owned()]);
    loop {
        let before = rerun.len();
        for task in tasks.iter().filter(|task| task.included) {
            if task.spec.needs.iter().any(|need| rerun.contains(need)) {
                rerun.insert(task.key.clone());
            }
        }
        if rerun.len() == before {
            return rerun;
        }
    }
}

fn parse_source_kind(value: &str) -> Result<SourceKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn parse_task_state(value: &str) -> Result<TaskState, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
