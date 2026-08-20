use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    AiModels, AiTool, AiUsageId, AiUsageState, ArtifactManifestEntry, AttemptId, AttemptState,
    ErrorCode, JobId, RequestId, RunnerId, RunnerTags, RunnerTools, Sha256Digest, TaskId,
    TaskLogLevel, UploadId, UsageOrigin,
};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RegisterRunnerRequest {
    pub tools: RunnerTools,
    pub ai_models: AiModels,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RegisterRunnerResponse {
    pub runner_id: RunnerId,
    pub token: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateRunnerSlot {
    pub name: String,
    pub tags: RunnerTags,
    pub max_concurrency: u16,
    pub default_model: Option<String>,
    pub default_effort: Option<String>,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CreateRunnerSlotResponse {
    pub runner_id: RunnerId,
    pub registration_token: String,
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

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskLogLine {
    pub timestamp_ms: u64,
    pub level: TaskLogLevel,
    pub message: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct LogCursor {
    pub last_sequence: u64,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskLogEvent {
    pub job_id: JobId,
    pub task_id: TaskId,
    pub attempt_id: AttemptId,
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
    pub applied: bool,
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

    #[test]
    fn usage_preserves_codex_tokens_and_qoder_credits() {
        let codex = UsageUpdate::Final {
            invocation_key: "codex-1".to_owned(),
            origin: UsageOrigin::Observed,
            input_tokens: Some(120),
            output_tokens: Some(30),
            cost_micros: None,
            credits_micros: None,
        };
        let qoder = UsageUpdate::Final {
            invocation_key: "qoder-1".to_owned(),
            origin: UsageOrigin::Observed,
            input_tokens: None,
            output_tokens: None,
            cost_micros: None,
            credits_micros: Some(2_500_000),
        };

        let codex_json = serde_json::to_string(&codex).expect("Codex usage");
        assert!(codex_json.contains(r#""input_tokens":120"#));
        assert!(codex_json.contains(r#""credits_micros":null"#));
        let qoder_json = serde_json::to_string(&qoder).expect("Qoder usage");
        assert!(qoder_json.contains(r#""input_tokens":null"#));
        assert!(qoder_json.contains(r#""output_tokens":null"#));
        assert!(qoder_json.contains(r#""credits_micros":2500000"#));
        assert!(!qoder_json.contains(r#""input_tokens":0"#));
        assert!(!qoder_json.contains(r#""output_tokens":0"#));
    }

    #[test]
    fn usage_ack_distinguishes_application_from_idempotent_replay() {
        let usage_id = AiUsageId::generate();
        let applied = UsageAck {
            usage_id,
            state: AiUsageState::Started,
            applied: true,
        };
        let replayed = UsageAck {
            applied: false,
            ..applied
        };

        assert!(
            serde_json::to_string(&applied)
                .expect("applied ack")
                .contains(r#""applied":true"#)
        );
        assert!(
            serde_json::to_string(&replayed)
                .expect("replayed ack")
                .contains(r#""applied":false"#)
        );
    }

    #[test]
    fn task_log_line_is_strict_and_closed() {
        let json = r#"{"timestamp_ms":1,"level":"warn","message":"retry"}"#;
        let line: TaskLogLine = serde_json::from_str(json).expect("strict task log line");
        assert_eq!(serde_json::to_string(&line).expect("wire JSON"), json);
        serde_json::from_str::<TaskLogLine>(&json.replace('}', ",\"extra\":1}"))
            .expect_err("unknown field");
    }

    #[test]
    fn task_log_event_contains_only_the_frozen_cursor() {
        let event = TaskLogEvent {
            job_id: JobId::generate(),
            task_id: TaskId::generate(),
            attempt_id: AttemptId::generate(),
            last_sequence: 7,
        };
        let json = serde_json::to_string(&event).expect("cursor event");
        assert!(json.contains("\"last_sequence\":7"));
        assert!(!json.contains("message"));
        assert!(!json.contains("sha256"));
        serde_json::from_str::<TaskLogEvent>(&json.replace('}', ",\"extra\":1}"))
            .expect_err("unknown field");
    }
}
