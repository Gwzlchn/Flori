use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{CollectionId, DomainId, Sha256Digest, SourceId, SourceInputId, SourceKind};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateUploadSource {
    pub request_key: String,
    pub kind: SourceKind,
    pub title: Option<String>,
    pub domain_id: DomainId,
    pub collection_ids: Vec<CollectionId>,
    pub file_sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateUploadSourceForm {
    pub metadata: CreateUploadSource,
    #[schema(value_type = String, format = Binary)]
    pub file: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PendingSourceCommit {
    pub source_id: SourceId,
    pub source_input_id: SourceInputId,
    pub kind: SourceKind,
    pub canonical_ref: String,
    pub title: Option<String>,
    pub domain_id: DomainId,
    pub collection_ids: Vec<CollectionId>,
    pub file_name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub final_relative_path: String,
    pub created_at_ms: i64,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upload_contract_rejects_unknown_fields() {
        let json = format!(
            r#"{{"request_key":"one","kind":"pdf_upload","title":null,"domain_id":"{}","collection_ids":[],"file_sha256":"{}","legacy":true}}"#,
            DomainId::generate(),
            "0".repeat(64),
        );
        serde_json::from_str::<CreateUploadSource>(&json).expect_err("unknown upload field");
    }
}
