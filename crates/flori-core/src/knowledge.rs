use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    ArtifactId, ArtifactKind, EvidenceId, EvidenceLocator, JobId, SearchChunkId, Sha256Digest,
    SourceId, TaskId,
};

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
