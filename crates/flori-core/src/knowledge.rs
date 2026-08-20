use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    ArtifactId, ArtifactKind, AttemptId, AttemptState, CompiledTaskSpec, DomainId, ErrorCode,
    EvidenceId, EvidenceLocator, Executor, JobId, JobInputs, JobState, JobTrigger,
    PipelineRevisionId, RunnerId, SearchChunkId, Sha256Digest, SourceId, SourceKind, TaskId,
    TaskState,
};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SourceView {
    pub source_id: SourceId,
    pub kind: SourceKind,
    pub canonical_ref: String,
    pub title: Option<String>,
    pub domain_id: DomainId,
    pub current_job_id: Option<JobId>,
    pub previous_job_id: Option<JobId>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct AttemptView {
    pub attempt_id: AttemptId,
    pub task_id: TaskId,
    pub attempt_no: u32,
    pub runner_id: Option<RunnerId>,
    pub state: AttemptState,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub runner_config_revision: Option<u64>,
    pub lease_expires_at_ms: u64,
    pub last_log_sequence: u64,
    pub started_at_ms: u64,
    pub finished_at_ms: Option<u64>,
    pub error_code: Option<ErrorCode>,
    pub error_message: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskView {
    pub task_id: TaskId,
    pub task_key: String,
    pub executor: Executor,
    pub state: TaskState,
    pub spec: CompiledTaskSpec,
    pub current_attempt_id: Option<AttemptId>,
    pub pinned_runner_id: Option<RunnerId>,
    pub selected_model: Option<String>,
    pub selected_effort: Option<String>,
    pub runner_config_revision: Option<u64>,
    pub error_code: Option<ErrorCode>,
    pub error_message: Option<String>,
    pub attempts: Vec<AttemptView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct JobView {
    pub job_id: JobId,
    pub source_id: SourceId,
    pub pipeline_revision_id: PipelineRevisionId,
    pub trigger: JobTrigger,
    pub state: JobState,
    pub inputs: JobInputs,
    pub error_code: Option<ErrorCode>,
    pub error_message: Option<String>,
    pub tasks: Vec<TaskView>,
    pub artifacts: Vec<ArtifactView>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactView {
    pub artifact_id: ArtifactId,
    pub source_id: SourceId,
    pub job_id: JobId,
    pub task_id: TaskId,
    pub name: String,
    pub kind: ArtifactKind,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SearchHit {
    pub chunk_id: SearchChunkId,
    pub source_id: SourceId,
    pub job_id: JobId,
    pub artifact_id: ArtifactId,
    pub title: String,
    pub body: String,
    pub evidence_ids: Vec<EvidenceId>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceView {
    pub evidence_id: EvidenceId,
    pub source_id: SourceId,
    pub job_id: JobId,
    pub source_artifact_id: ArtifactId,
    pub locator: EvidenceLocator,
    pub quote: String,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_and_evidence_views_reject_contract_drift() {
        let source = format!(
            r#"{{"source_id":"{}","kind":"pdf_upload","canonical_ref":"upload:test","title":null,"domain_id":"{}","current_job_id":null,"previous_job_id":null,"legacy":true}}"#,
            SourceId::generate(),
            DomainId::generate(),
        );
        serde_json::from_str::<SourceView>(&source).expect_err("unknown source field");
        let artifact = format!(
            r#"{{"artifact_id":"{}","source_id":"{}","job_id":"{}","task_id":"{}","name":"note","kind":"smart_note","media_type":"text/markdown","size_bytes":1,"sha256":"{}","extra":true}}"#,
            ArtifactId::generate(),
            SourceId::generate(),
            JobId::generate(),
            TaskId::generate(),
            "0".repeat(64),
        );
        serde_json::from_str::<ArtifactView>(&artifact).expect_err("unknown artifact field");
        let json = format!(
            r#"{{"chunk_id":"{}","source_id":"{}","job_id":"{}","artifact_id":"{}","title":"t","body":"b","evidence_ids":[],"extra":true}}"#,
            SearchChunkId::generate(),
            SourceId::generate(),
            JobId::generate(),
            ArtifactId::generate(),
        );
        serde_json::from_str::<SearchHit>(&json).expect_err("unknown search field");
    }
}
