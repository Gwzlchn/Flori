use flori_core::{ArtifactId, ErrorCode, JobId, SourceId, SourceInputId, TaskId};

use super::ArtifactStoreError;

pub fn source_input_path(
    source_id: SourceId,
    source_input_id: SourceInputId,
    file_name: &str,
) -> Result<String, ArtifactStoreError> {
    validate_name(file_name)?;
    Ok(format!(
        "sources/{source_id}/inputs/{source_input_id}/{file_name}"
    ))
}

pub fn retained_artifact_path(
    source_id: SourceId,
    artifact_id: ArtifactId,
    file_name: &str,
) -> Result<String, ArtifactStoreError> {
    validate_name(file_name)?;
    Ok(format!(
        "sources/{source_id}/retained/{artifact_id}/{file_name}"
    ))
}

pub fn task_artifact_path(
    source_id: SourceId,
    job_id: JobId,
    task_id: TaskId,
    artifact_id: ArtifactId,
    file_name: &str,
) -> Result<String, ArtifactStoreError> {
    validate_name(file_name)?;
    Ok(format!(
        "sources/{source_id}/jobs/{job_id}/tasks/{task_id}/{artifact_id}/{file_name}"
    ))
}

pub(super) fn validate_name(name: &str) -> Result<(), ArtifactStoreError> {
    if name.is_empty() || name.starts_with('.') || name.contains(['/', '\\', '\0']) {
        return Err(ArtifactStoreError::with_code(
            ErrorCode::ArtifactInvalidPath,
        ));
    }
    Ok(())
}

pub(super) fn validate_final_path(path: &str) -> Result<(), ArtifactStoreError> {
    let segments: Vec<_> = path.split('/').collect();
    if segments
        .iter()
        .any(|segment| validate_name(segment).is_err())
    {
        return Err(ArtifactStoreError::with_code(
            ErrorCode::ArtifactInvalidPath,
        ));
    }
    let valid = match segments.as_slice() {
        ["sources", source, "inputs", input, _] => {
            source.parse::<SourceId>().is_ok() && input.parse::<SourceInputId>().is_ok()
        }
        ["sources", source, "retained", artifact, _] => {
            source.parse::<SourceId>().is_ok() && artifact.parse::<ArtifactId>().is_ok()
        }
        ["sources", source, "jobs", job, "tasks", task, artifact, _] => {
            source.parse::<SourceId>().is_ok()
                && job.parse::<JobId>().is_ok()
                && task.parse::<TaskId>().is_ok()
                && artifact.parse::<ArtifactId>().is_ok()
        }
        _ => false,
    };
    valid
        .then_some(())
        .ok_or_else(|| ArtifactStoreError::with_code(ErrorCode::ArtifactInvalidPath))
}
