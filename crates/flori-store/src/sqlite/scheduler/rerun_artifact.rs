use std::{collections::BTreeMap, path::Path, str::FromStr};

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactRetention, ArtifactWhen, ErrorCode,
    JobId, PendingMaterializedArtifact, PendingTaskCommit, Sha256Digest, SourceId, TaskId,
    TaskState, UploadId,
};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::task_artifact_path;

use super::super::StoreError;

#[allow(clippy::too_many_arguments)]
pub(super) async fn plan_artifacts(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: SourceId,
    base_job_id: JobId,
    job_id: JobId,
    base_tasks: &BTreeMap<String, (TaskId, TaskState, Option<String>)>,
    tasks: &[PendingTaskCommit],
    materialize_keys: &[String],
    reuse: Option<&[PendingMaterializedArtifact]>,
) -> Result<Vec<PendingMaterializedArtifact>, StoreError> {
    let mut planned = Vec::new();
    for key in materialize_keys {
        let task = tasks
            .iter()
            .find(|task| task.task_key == *key)
            .ok_or_else(corrupt)?;
        let (base_task_id, base_state, current_attempt) =
            base_tasks.get(key).ok_or_else(corrupt)?;
        let rows = sqlx::query(
            "SELECT id,attempt_id,origin,materialized_from_artifact_id,name,kind,media_type, \
             file_name,size_bytes,sha256,relative_path,retention FROM artifacts \
             WHERE source_id=? AND job_id=? AND task_id=? ORDER BY name",
        )
        .bind(source_id.to_string())
        .bind(base_job_id.to_string())
        .bind(base_task_id.to_string())
        .fetch_all(&mut **transaction)
        .await?;
        let mut counts = BTreeMap::<String, u16>::new();
        for row in rows {
            let retention = parse_retention(row.try_get("retention")?)?;
            if retention == ArtifactRetention::FailedAudit {
                continue;
            }
            let attempt: Option<String> = row.try_get("attempt_id")?;
            let origin: String = row.try_get("origin")?;
            let materialized_from: Option<String> = row.try_get("materialized_from_artifact_id")?;
            match base_state {
                TaskState::Succeeded
                    if origin == "produced"
                        && attempt.as_deref() == current_attempt.as_deref()
                        && materialized_from.is_none() => {}
                TaskState::Skipped
                    if origin == "materialized"
                        && attempt.is_none()
                        && materialized_from.is_some() => {}
                _ => return Err(corrupt()),
            }
            let name: String = row.try_get("name")?;
            let (declaration, basename) = declaration(&task.spec.artifacts, &name)?;
            if declaration.when != ArtifactWhen::OnSuccess {
                return Err(corrupt());
            }
            let kind = parse_kind(row.try_get("kind")?)?;
            let media_type: String = row.try_get("media_type")?;
            let size_bytes = to_u64(row.try_get("size_bytes")?)?;
            let file_name: String = row.try_get("file_name")?;
            if kind != declaration.kind
                || !kind.accepts_media_type(&media_type)
                || size_bytes > declaration.max_bytes
                || file_name != basename
                || Path::new(row.try_get::<String, _>("relative_path")?.as_str())
                    .file_name()
                    .and_then(|value| value.to_str())
                    != Some(file_name.as_str())
            {
                return Err(corrupt());
            }
            let count = counts.entry(declaration.name.clone()).or_default();
            *count = count.checked_add(1).ok_or_else(corrupt)?;
            if *count > declaration.max_files.unwrap_or(1) {
                return Err(corrupt());
            }
            let source_artifact_id =
                ArtifactId::from_str(row.try_get("id")?).map_err(|_| corrupt())?;
            let existing = reuse.and_then(|artifacts| {
                artifacts
                    .iter()
                    .find(|artifact| artifact.source_artifact_id == source_artifact_id)
            });
            let artifact_id = existing.map_or_else(ArtifactId::generate, |item| item.artifact_id);
            let upload_id = existing.map_or_else(UploadId::generate, |item| item.upload_id);
            let final_relative_path =
                task_artifact_path(source_id, job_id, task.task_id, artifact_id, &file_name)
                    .map_err(|error| StoreError::new(error.code()))?;
            planned.push(PendingMaterializedArtifact {
                upload_id,
                artifact_id,
                source_artifact_id,
                task_id: task.task_id,
                name,
                kind,
                media_type,
                file_name,
                size_bytes,
                sha256: Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
                    .map_err(|_| corrupt())?,
                retention,
                final_relative_path,
            });
        }
        for declaration in task
            .spec
            .artifacts
            .iter()
            .filter(|item| item.when == ArtifactWhen::OnSuccess && item.required)
        {
            if counts.get(&declaration.name).copied().unwrap_or(0) == 0 {
                return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
            }
        }
    }
    planned.sort_by(|left, right| (left.task_id, &left.name).cmp(&(right.task_id, &right.name)));
    if reuse.is_some_and(|artifacts| artifacts.len() != planned.len()) {
        return Err(corrupt());
    }
    Ok(planned)
}

fn declaration<'a>(
    declarations: &'a [ArtifactDeclaration],
    name: &str,
) -> Result<(&'a ArtifactDeclaration, String), StoreError> {
    for declaration in declarations {
        if declaration.max_files.is_none() && name == declaration.name {
            let basename = Path::new(&declaration.path)
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(corrupt)?;
            return Ok((declaration, basename.to_owned()));
        }
        if declaration.max_files.is_some()
            && let Some(basename) = name
                .strip_prefix(&declaration.name)
                .and_then(|suffix| suffix.strip_prefix('/'))
            && !basename.is_empty()
            && !basename.starts_with('.')
            && !basename.contains(['/', '\\', '\0'])
        {
            return Ok((declaration, basename.to_owned()));
        }
    }
    Err(corrupt())
}

fn parse_kind(value: &str) -> Result<ArtifactKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn parse_retention(value: &str) -> Result<ArtifactRetention, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn to_u64(value: i64) -> Result<u64, StoreError> {
    value.try_into().map_err(|_| corrupt())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
