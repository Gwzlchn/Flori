use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    AiRunnerSelection, ArtifactId, ArtifactKind, ArtifactManifestEntry, ArtifactRetention,
    CompiledTaskSpec, JobId, JobInputs, PipelineRevisionId, PromptSnapshot, PromptSnapshotId,
    Sha256Digest, SourceId, TaskId, TaskInputBindings, TaskState, UploadId,
};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingTaskCommit {
    pub task_id: TaskId,
    pub task_key: String,
    pub spec: CompiledTaskSpec,
    pub bindings: TaskInputBindings,
    pub state: TaskState,
    pub ai_selection: Option<AiRunnerSelection>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingAttemptUpload {
    pub artifact_id: ArtifactId,
    pub declaration_name: String,
    pub artifact: ArtifactManifestEntry,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingMaterializedArtifact {
    pub upload_id: UploadId,
    pub artifact_id: ArtifactId,
    pub source_artifact_id: ArtifactId,
    pub task_id: TaskId,
    pub name: String,
    pub kind: ArtifactKind,
    pub media_type: String,
    pub file_name: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub retention: ArtifactRetention,
    pub final_relative_path: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingMaterializeCommit {
    pub source_id: SourceId,
    pub base_job_id: JobId,
    pub job_id: JobId,
    pub pipeline_revision_id: PipelineRevisionId,
    pub prompt_snapshot_id: PromptSnapshotId,
    pub prompt_snapshot: PromptSnapshot,
    pub inputs: JobInputs,
    pub from_task_key: String,
    pub created_at_ms: i64,
    pub tasks: Vec<PendingTaskCommit>,
    pub artifacts: Vec<PendingMaterializedArtifact>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn materialize_commit_rejects_unknown_fields_before_recovery() {
        let json = format!(
            r#"{{"source_id":"{}","base_job_id":"{}","job_id":"{}","pipeline_revision_id":"{}","prompt_snapshot_id":"{}","prompt_snapshot":{{"profile":{{"domain_id":"{}","profile_text":"","sha256":"{}"}},"prompts":[]}},"inputs":{{"translate":false}},"from_task_key":"note","created_at_ms":1,"tasks":[],"artifacts":[],"legacy":true}}"#,
            SourceId::generate(),
            JobId::generate(),
            JobId::generate(),
            PipelineRevisionId::generate(),
            PromptSnapshotId::generate(),
            crate::DomainId::generate(),
            "0".repeat(64),
        );
        serde_json::from_str::<PendingMaterializeCommit>(&json).expect_err("unknown field");
    }

    #[test]
    fn attempt_upload_freezes_only_the_server_manifest_entry() {
        let json = format!(
            r#"{{"artifact_id":"{}","declaration_name":"figures","artifact":{{"name":"figures/one.png","kind":"figure","media_type":"image/png","size_bytes":3,"sha256":"{}","relative_path":"relative"}},"path":"runner-owned"}}"#,
            ArtifactId::generate(),
            "0".repeat(64),
        );
        serde_json::from_str::<PendingAttemptUpload>(&json).expect_err("unknown path field");
    }
}
