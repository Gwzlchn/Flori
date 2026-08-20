use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    AiModelCapability, AiTool, AiUsageId, AiUsageState, ArtifactManifestEntry, AttemptId,
    AttemptState, ErrorCode, RequestId, RunnerId, RunnerToolCapability, Sha256Digest, UploadId,
    UsageOrigin,
};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RegisterRunnerRequest {
    pub tools: Vec<RunnerToolCapability>,
    pub ai_models: Vec<AiModelCapability>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RegisterRunnerResponse {
    pub runner_id: RunnerId,
    pub token: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RenewLeaseResponse {
    pub lease_expires_at_ms: i64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct LogFrame {
    pub sequence: u64,
    pub sha256: Sha256Digest,
    pub line: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct LogCursor {
    pub last_sequence: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(tag = "state", rename_all = "snake_case", deny_unknown_fields)]
pub enum UsageUpdate {
    Started {
        invocation_key: String,
        tool: AiTool,
        model: String,
        effort: String,
    },
    Final {
        invocation_key: String,
        origin: UsageOrigin,
        input_tokens: Option<u64>,
        output_tokens: Option<u64>,
        cost_micros: Option<u64>,
        credits_micros: Option<u64>,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct UsageAck {
    pub usage_id: AiUsageId,
    pub state: AiUsageState,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct StartUploadRequest {
    pub name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct StartUploadResponse {
    pub upload_id: UploadId,
    pub received_bytes: u64,
    pub artifact: ArtifactManifestEntry,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct UploadCursor {
    pub upload_id: UploadId,
    pub received_bytes: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct VerifyUploadRequest {
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct VerifyUploadResponse {
    pub upload_id: UploadId,
    pub artifact: ArtifactManifestEntry,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CompleteAttemptRequest {
    pub manifest_sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct FailAttemptRequest {
    pub error_code: ErrorCode,
    pub manifest_sha256: Option<Sha256Digest>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct AttemptAck {
    pub exec_id: AttemptId,
    pub state: AttemptState,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ErrorResponse {
    pub error: ErrorBody,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ErrorBody {
    pub code: ErrorCode,
    pub message: String,
    pub request_id: RequestId,
    pub field: Option<String>,
    pub retry_after_ms: Option<u64>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn usage_update_is_strict_and_tagged() {
        let json = r#"{"state":"started","invocation_key":"one","tool":"qoder_cli","model":"m","effort":"high"}"#;
        let update: UsageUpdate = serde_json::from_str(json).expect("strict update");
        assert_eq!(serde_json::to_string(&update).expect("wire JSON"), json);
        serde_json::from_str::<UsageUpdate>(&json.replace('}', ",\"extra\":1}"))
            .expect_err("unknown field");
    }
}
