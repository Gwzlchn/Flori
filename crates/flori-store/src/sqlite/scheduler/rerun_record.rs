use flori_core::{
    ArtifactKind, ErrorCode, PendingMaterializeCommit, PendingMaterializedArtifact, Sha256Digest,
    UploadId, UploadState,
};
use sqlx::Row;

use crate::artifact::UploadRecord;

use super::{
    super::StoreError,
    rerun_artifact::{declaration, parse_upload_state, source_visible, to_u64},
};

pub(in crate::sqlite) async fn load_records(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
    artifact: &PendingMaterializedArtifact,
    request_key: &str,
    request_sha256: &str,
) -> Result<(UploadRecord, UploadRecord), StoreError> {
    let row = sqlx::query(
        "SELECT u.owner_id,u.request_key,u.request_sha256,u.commit_json,u.name,u.target_id, \
         u.source_artifact_id,u.staging_path,u.final_relative_path,u.expected_size_bytes, \
         u.expected_sha256,u.received_bytes,u.state,a.source_id,a.job_id,a.attempt_id,a.origin, \
         a.materialized_from_artifact_id,a.name AS source_name,a.kind AS source_kind, \
         a.media_type AS source_media_type,a.file_name AS source_file_name, \
         a.relative_path AS source_path,a.size_bytes AS source_size,a.sha256 AS source_sha, \
         a.retention AS source_retention,t.task_key AS source_task_key,t.state AS source_task_state, \
         t.current_attempt_id AS source_current_attempt,t.id AS source_task_id, \
         x.task_id AS attempt_task_id,x.state AS attempt_state FROM uploads u \
         JOIN artifacts a ON a.id=u.source_artifact_id JOIN tasks t ON t.id=a.task_id \
         LEFT JOIN attempts x ON x.id=a.attempt_id \
         WHERE u.id=? AND u.owner_kind='materialize'",
    )
    .bind(artifact.upload_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(corrupt)?;
    let decoded: PendingMaterializeCommit =
        serde_json::from_str(row.try_get("commit_json")?).map_err(|_| corrupt())?;
    let task_key = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .map(|task| task.task_key.as_str())
        .ok_or_else(corrupt)?;
    let attempt: Option<String> = row.try_get("attempt_id")?;
    let current_attempt: Option<String> = row.try_get("source_current_attempt")?;
    let materialized_from: Option<String> = row.try_get("materialized_from_artifact_id")?;
    let visible = source_visible(
        row.try_get("origin")?,
        row.try_get("source_task_state")?,
        (
            attempt.as_deref(),
            current_attempt.as_deref(),
            materialized_from.as_deref(),
        ),
        (
            row.try_get("source_task_id")?,
            row.try_get::<Option<String>, _>("attempt_task_id")?
                .as_deref(),
            row.try_get::<Option<String>, _>("attempt_state")?
                .as_deref(),
        ),
    );
    let expected_request_key = pending
        .artifacts
        .first()
        .is_some_and(|first| artifact.upload_id == first.upload_id)
        .then_some(request_key);
    let expected_kind = serde_json::to_string(&artifact.kind).map_err(|_| corrupt())?;
    let expected_retention = serde_json::to_string(&artifact.retention).map_err(|_| corrupt())?;
    let source_size = to_u64(row.try_get("source_size")?)?;
    let source_sha =
        Sha256Digest::parse(row.try_get::<String, _>("source_sha")?).map_err(|_| corrupt())?;
    let rewritten = matches!(
        artifact.kind,
        ArtifactKind::DocumentStructure | ArtifactKind::Evidence
    );
    if decoded != *pending
        || row.try_get::<String, _>("owner_id")? != pending.job_id.to_string()
        || row.try_get::<Option<String>, _>("request_key")?.as_deref() != expected_request_key
        || row
            .try_get::<Option<String>, _>("request_sha256")?
            .as_deref()
            != Some(request_sha256)
        || row.try_get::<String, _>("name")? != format!("{task_key}/{}", artifact.name)
        || row.try_get::<String, _>("target_id")? != artifact.artifact_id.to_string()
        || row
            .try_get::<Option<String>, _>("source_artifact_id")?
            .as_deref()
            != Some(artifact.source_artifact_id.to_string().as_str())
        || row.try_get::<String, _>("final_relative_path")? != artifact.final_relative_path
        || to_u64(row.try_get("expected_size_bytes")?)? != artifact.size_bytes
        || row.try_get::<String, _>("expected_sha256")? != artifact.sha256.as_str()
        || !visible
        || row.try_get::<String, _>("source_id")? != pending.source_id.to_string()
        || row.try_get::<String, _>("job_id")? != pending.base_job_id.to_string()
        || row.try_get::<String, _>("source_task_key")? != task_key
        || row.try_get::<String, _>("source_name")? != artifact.name
        || row.try_get::<String, _>("source_kind")? != expected_kind.trim_matches('"')
        || row.try_get::<String, _>("source_media_type")? != artifact.media_type
        || row.try_get::<String, _>("source_file_name")? != artifact.file_name
        || !rewritten && source_size != artifact.size_bytes
        || !rewritten && source_sha != artifact.sha256
        || row.try_get::<String, _>("source_retention")? != expected_retention.trim_matches('"')
    {
        return Err(corrupt());
    }
    let task = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .ok_or_else(corrupt)?;
    let (declaration, _) = declaration(&task.spec.artifacts, &artifact.name)?;
    let mut target = UploadRecord::new(
        artifact.upload_id,
        &artifact.name,
        &artifact.final_relative_path,
        artifact.size_bytes,
        artifact.sha256.clone(),
        &declaration.name,
        declaration.max_bytes,
    )
    .map_err(|_| corrupt())?;
    target
        .restore_progress(
            to_u64(row.try_get("received_bytes")?)?,
            parse_upload_state(row.try_get("state")?)?,
        )
        .map_err(|_| corrupt())?;
    if target.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    let mut source = UploadRecord::new(
        UploadId::generate(),
        "source",
        row.try_get::<String, _>("source_path")?,
        source_size,
        source_sha,
        "source",
        source_size,
    )
    .map_err(|_| corrupt())?;
    source
        .restore_progress(source_size, UploadState::Moved)
        .map_err(|_| corrupt())?;
    Ok((target, source))
}

pub(in crate::sqlite) async fn validate_owner(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    pending: &PendingMaterializeCommit,
) -> Result<(), StoreError> {
    let valid: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM sources s JOIN jobs j ON j.id=s.current_job_id \
         JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id \
         WHERE s.id=? AND s.current_job_id=? AND j.state='succeeded' \
         AND j.pipeline_revision_id=?",
    )
    .bind(pending.source_id.to_string())
    .bind(pending.base_job_id.to_string())
    .bind(pending.pipeline_revision_id.to_string())
    .fetch_one(&mut **transaction)
    .await?;
    if valid != 1 {
        return Err(StoreError::new(ErrorCode::RerunBoundaryInvalid));
    }
    Ok(())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
