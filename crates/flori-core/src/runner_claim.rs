use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, AttemptId, CredentialKind, DomainId, Executor,
    JobId, RunnerTool, Sha256Digest, SourceId, SourceInputId, SourceKind, TaskId,
};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct RunnerToolCapability {
    pub tool: RunnerTool,
    pub version: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct AiModelCapability {
    pub model: String,
    pub efforts: Vec<String>,
}

pub type RunnerTags = Vec<String>;
pub type RunnerTools = Vec<RunnerToolCapability>;
pub type AiModels = Vec<AiModelCapability>;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ResolvedArtifact {
    pub artifact_id: ArtifactId,
    pub name: String,
    pub kind: ArtifactKind,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub download_url: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ResolvedSourceInput {
    pub source_input_id: SourceInputId,
    pub name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub download_url: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ResolvedSource {
    pub source_id: SourceId,
    pub kind: SourceKind,
    pub canonical_ref: String,
    pub input: Option<ResolvedSourceInput>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ResolvedPrompt {
    pub key: String,
    pub content: String,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ResolvedProfile {
    pub domain_id: DomainId,
    pub content: String,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(tag = "executor", content = "inputs")]
pub enum ResolvedTaskInputs {
    #[serde(rename = "document.acquire")]
    DocumentAcquire { source: ResolvedSource },
    #[serde(rename = "document.extract")]
    DocumentExtract { pdf: ResolvedArtifact },
    #[serde(rename = "ai.document_translate")]
    AiDocumentTranslate {
        document: ResolvedArtifact,
        prompt: ResolvedPrompt,
        profile: Option<ResolvedProfile>,
    },
    #[serde(rename = "ai.document_note")]
    AiDocumentNote {
        document: ResolvedArtifact,
        prompt: ResolvedPrompt,
        profile: Option<ResolvedProfile>,
    },
    #[serde(rename = "video.acquire")]
    VideoAcquire { source: ResolvedSource },
    #[serde(rename = "video.subscription")]
    VideoSubscription { source: ResolvedSource },
    #[serde(rename = "video.transcribe")]
    VideoTranscribe {
        video: ResolvedArtifact,
        subtitle: Option<ResolvedArtifact>,
    },
    #[serde(rename = "video.frames")]
    VideoFrames {
        video: ResolvedArtifact,
        transcript: ResolvedArtifact,
    },
    #[serde(rename = "video.mechanical_note")]
    VideoMechanicalNote {
        transcript: ResolvedArtifact,
        frames: Vec<ResolvedArtifact>,
    },
    #[serde(rename = "ai.video_note")]
    AiVideoNote {
        transcript: ResolvedArtifact,
        mechanical_note: ResolvedArtifact,
        frames: Vec<ResolvedArtifact>,
        prompt: ResolvedPrompt,
        profile: Option<ResolvedProfile>,
    },
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SecretCredential {
    pub kind: CredentialKind,
    pub value: String,
}

#[derive(Clone, Default, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SecretInputs {
    pub credential: Option<SecretCredential>,
}

#[derive(Clone, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TaskClaim {
    pub job_id: JobId,
    pub task_id: TaskId,
    pub task_key: String,
    pub exec_id: AttemptId,
    pub attempt_no: u8,
    pub executor: Executor,
    pub timeout_ms: u64,
    pub lease_expires_at_ms: i64,
    pub resolved_inputs: ResolvedTaskInputs,
    pub output_declarations: Vec<ArtifactDeclaration>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub runner_config_revision: u64,
    pub secret_inputs: SecretInputs,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn capability_rejects_unknown_fields() {
        let json = r#"{"tool":"ffmpeg","version":"7.1","extra":true}"#;
        serde_json::from_str::<RunnerToolCapability>(json).expect_err("unknown field");
    }

    #[test]
    fn secrets_have_no_debug_representation() {
        fn accepts_wire<T: Serialize + for<'de> Deserialize<'de>>() {}
        accepts_wire::<SecretInputs>();
    }
}
