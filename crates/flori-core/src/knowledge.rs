use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{ArtifactId, EvidenceId, EvidenceLocator, JobId, SearchChunkId, SourceId};

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
