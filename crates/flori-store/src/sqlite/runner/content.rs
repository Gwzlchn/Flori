use std::str::FromStr;

use flori_core::{
    ArtifactId, ErrorCode, Executor, ResolvedArtifact, ResolvedSourceInput, ResolvedTaskInputs,
    RunnerId, Sha256Digest, SourceInputId, TaskInputBindings,
};
use sqlx::Row;

use crate::artifact::{retained_artifact_path, source_input_path, task_artifact_path};

use super::{
    super::{Store, StoreError},
    resolve::resolved_inputs,
};

impl Store {
    pub async fn authorize_artifact_content(
        &self,
        runner_id: RunnerId,
        artifact_id: ArtifactId,
        now_ms: i64,
    ) -> Result<(String, String, u64, Sha256Digest), StoreError> {
        let mut transaction = self.pool.begin().await?;
        let attempts = active_inputs(&mut transaction, runner_id, now_ms).await?;
        let mut authorized = false;
        for (job_id, bindings) in attempts {
            let resolved = resolved_inputs(&mut transaction, &job_id, &bindings, "").await?;
            if contains_artifact(&resolved, artifact_id) {
                authorized = true;
                break;
            }
        }
        if !authorized {
            return Err(StoreError::new(ErrorCode::NotFound));
        }
        let row = sqlx::query(
            "SELECT source_id,job_id,task_id,file_name,retention,media_type,size_bytes,sha256, \
             relative_path FROM artifacts WHERE id=?",
        )
        .bind(artifact_id.to_string())
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))?;
        artifact_metadata(row, artifact_id)
    }

    pub async fn authorize_source_input_content(
        &self,
        runner_id: RunnerId,
        source_input_id: SourceInputId,
        now_ms: i64,
    ) -> Result<(String, String, u64, Sha256Digest), StoreError> {
        let mut transaction = self.pool.begin().await?;
        let attempts = active_inputs(&mut transaction, runner_id, now_ms).await?;
        let mut authorized = false;
        for (job_id, bindings) in attempts {
            let resolved = resolved_inputs(&mut transaction, &job_id, &bindings, "").await?;
            if contains_source_input(&resolved, source_input_id) {
                authorized = true;
                break;
            }
        }
        if !authorized {
            return Err(StoreError::new(ErrorCode::NotFound));
        }
        let row = sqlx::query(
            "SELECT source_id,name,media_type,size_bytes,sha256,relative_path \
             FROM source_inputs WHERE id=?",
        )
        .bind(source_input_id.to_string())
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))?;
        source_input_metadata(row, source_input_id)
    }
}

async fn active_inputs(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    runner_id: RunnerId,
    now_ms: i64,
) -> Result<Vec<(String, TaskInputBindings)>, StoreError> {
    if now_ms < 0 {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    let rows = sqlx::query(
        "SELECT t.job_id,t.executor,t.input_bindings_json FROM attempts a \
         JOIN runners r ON r.id=a.runner_id JOIN tasks t ON t.id=a.task_id \
         JOIN jobs j ON j.id=t.job_id \
         WHERE a.runner_id=? AND a.state='leased' AND a.lease_expires_at_ms>? \
         AND r.state='enabled' AND r.token_digest IS NOT NULL \
         AND a.started_at_ms>? - t.timeout_ms \
         AND t.current_attempt_id=a.id AND t.state='leased' AND j.state='running' \
         ORDER BY a.started_at_ms,a.id",
    )
    .bind(runner_id.to_string())
    .bind(now_ms)
    .bind(now_ms)
    .fetch_all(&mut **transaction)
    .await?;
    rows.into_iter()
        .map(|row| {
            let bindings: String = row.try_get("input_bindings_json")?;
            let executor: String = row.try_get("executor")?;
            let executor: Executor = serde_json::from_str(&format!("\"{executor}\""))
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            let bindings: TaskInputBindings = serde_json::from_str(&bindings)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            if bindings.executor() != executor || !bindings.is_valid() {
                return Err(StoreError::new(ErrorCode::CorruptState));
            }
            Ok((row.try_get("job_id")?, bindings))
        })
        .collect()
}

fn artifact_metadata(
    row: sqlx::sqlite::SqliteRow,
    artifact_id: ArtifactId,
) -> Result<(String, String, u64, Sha256Digest), StoreError> {
    let source_id = parse_id(row.try_get("source_id")?)?;
    let relative_path: String = row.try_get("relative_path")?;
    let file_name: String = row.try_get("file_name")?;
    let expected_path = match row.try_get::<String, _>("retention")?.as_str() {
        "source" => retained_artifact_path(source_id, artifact_id, &file_name),
        "published" | "failed_audit" => task_artifact_path(
            source_id,
            parse_id(row.try_get("job_id")?)?,
            parse_id(row.try_get("task_id")?)?,
            artifact_id,
            &file_name,
        ),
        _ => return Err(StoreError::new(ErrorCode::CorruptState)),
    }
    .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
    if relative_path != expected_path {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    common_metadata(row, relative_path)
}

fn source_input_metadata(
    row: sqlx::sqlite::SqliteRow,
    source_input_id: SourceInputId,
) -> Result<(String, String, u64, Sha256Digest), StoreError> {
    let relative_path: String = row.try_get("relative_path")?;
    let expected_path = source_input_path(
        parse_id(row.try_get("source_id")?)?,
        source_input_id,
        row.try_get("name")?,
    )
    .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
    if relative_path != expected_path {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    common_metadata(row, relative_path)
}

fn common_metadata(
    row: sqlx::sqlite::SqliteRow,
    relative_path: String,
) -> Result<(String, String, u64, Sha256Digest), StoreError> {
    let size: i64 = row.try_get("size_bytes")?;
    Ok((
        relative_path,
        row.try_get("media_type")?,
        size.try_into()
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?,
        Sha256Digest::parse(row.try_get::<String, _>("sha256")?)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?,
    ))
}

fn parse_id<T: FromStr>(value: String) -> Result<T, StoreError> {
    value
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn contains_artifact(inputs: &ResolvedTaskInputs, target: ArtifactId) -> bool {
    let matches = |artifact: &ResolvedArtifact| artifact.artifact_id == target;
    match inputs {
        ResolvedTaskInputs::DocumentAcquire { .. }
        | ResolvedTaskInputs::VideoAcquire { .. }
        | ResolvedTaskInputs::VideoSubscription { .. } => false,
        ResolvedTaskInputs::DocumentExtract { pdf } => matches(pdf),
        ResolvedTaskInputs::AiDocumentTranslate { document, .. }
        | ResolvedTaskInputs::AiDocumentNote { document, .. } => matches(document),
        ResolvedTaskInputs::VideoTranscribe { video, subtitle } => {
            matches(video) || subtitle.as_ref().is_some_and(matches)
        }
        ResolvedTaskInputs::VideoFrames { video, transcript } => {
            matches(video) || matches(transcript)
        }
        ResolvedTaskInputs::VideoMechanicalNote { transcript, frames } => {
            matches(transcript) || frames.iter().any(matches)
        }
        ResolvedTaskInputs::AiVideoNote {
            transcript,
            mechanical_note,
            frames,
            ..
        } => matches(transcript) || matches(mechanical_note) || frames.iter().any(matches),
    }
}

fn contains_source_input(inputs: &ResolvedTaskInputs, target: SourceInputId) -> bool {
    let matches = |input: &ResolvedSourceInput| input.source_input_id == target;
    match inputs {
        ResolvedTaskInputs::DocumentAcquire { source }
        | ResolvedTaskInputs::VideoAcquire { source }
        | ResolvedTaskInputs::VideoSubscription { source } => {
            source.input.as_ref().is_some_and(matches)
        }
        ResolvedTaskInputs::DocumentExtract { .. }
        | ResolvedTaskInputs::AiDocumentTranslate { .. }
        | ResolvedTaskInputs::AiDocumentNote { .. }
        | ResolvedTaskInputs::VideoTranscribe { .. }
        | ResolvedTaskInputs::VideoFrames { .. }
        | ResolvedTaskInputs::VideoMechanicalNote { .. }
        | ResolvedTaskInputs::AiVideoNote { .. } => false,
    }
}
