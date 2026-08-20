use flori_core::{ArtifactKind, CompiledTaskSpec, PendingAttemptUpload, Sha256Digest, UploadId};
use sqlx::{Row, sqlite::SqliteRow};

use crate::{
    artifact::{UploadRecord, task_artifact_path},
    sqlite::{StoreError, runner::upload_rule::declaration},
};

pub(in crate::sqlite) fn decode_record(
    row: &SqliteRow,
    upload_id: UploadId,
    spec: &CompiledTaskSpec,
) -> Result<UploadRecord, StoreError> {
    let name: String = row.try_get("name")?;
    let (declared, basename) = declaration(spec, &name).map_err(|_| corrupt())?;
    let commit: Option<String> = row.try_get("commit_json")?;
    let (size, sha256, declaration_name, final_path) = if let Some(json) = commit {
        let pending: PendingAttemptUpload = serde_json::from_str(&json).map_err(|_| corrupt())?;
        if serde_json::to_string(&pending).map_err(|_| corrupt())? != json
            || pending.artifact_id.to_string() != row.try_get::<String, _>("target_id")?
            || pending.artifact.name != name
            || pending.declaration_name != declared.name
            || pending.artifact.kind != declared.kind
            || !declared
                .kind
                .accepts_media_type(&pending.artifact.media_type)
            || i64::try_from(pending.artifact.size_bytes).map_err(|_| corrupt())?
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
                != i64::try_from(declared.max_bytes).map_err(|_| corrupt())?
        {
            return Err(corrupt());
        }
        (
            declared.max_bytes,
            Sha256Digest::parse(row.try_get::<String, _>("expected_sha256")?)
                .map_err(|_| corrupt())?,
            declared.name.clone(),
            task_artifact_path(
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
            .map_err(|_| corrupt())?,
        )
    };
    if final_path != row.try_get::<String, _>("final_relative_path")? {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        name,
        final_path,
        size,
        sha256,
        &declaration_name,
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
    StoreError::new(flori_core::ErrorCode::CorruptState)
}
