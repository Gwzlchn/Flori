use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{ArtifactDeclaration, DomainId, Executor, Sha256Digest, SourceKind};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PromptSnapshotPrompt {
    pub key: String,
    pub content: String,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PromptSnapshotProfile {
    pub domain_id: DomainId,
    pub profile_text: String,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PromptSnapshot {
    pub profile: PromptSnapshotProfile,
    pub prompts: Vec<PromptSnapshotPrompt>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct CompiledTaskSpec {
    pub executor: Executor,
    pub needs: Vec<String>,
    pub tags: Vec<String>,
    pub retry: u8,
    pub timeout_ms: u64,
    pub artifacts: Vec<ArtifactDeclaration>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum TaskInputReference {
    Source,
    JobTranslate,
    DomainProfile,
    Prompt(String),
    Need(String),
    NeedArtifact { task: String, artifact: String },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(tag = "executor", content = "inputs", deny_unknown_fields)]
pub enum TaskInputBindings {
    #[serde(rename = "document.acquire")]
    DocumentAcquire { source: TaskInputReference },
    #[serde(rename = "document.extract")]
    DocumentExtract { pdf: TaskInputReference },
    #[serde(rename = "ai.document_translate")]
    AiDocumentTranslate {
        document: TaskInputReference,
        prompt: TaskInputReference,
        profile: Option<TaskInputReference>,
    },
    #[serde(rename = "ai.document_note")]
    AiDocumentNote {
        document: TaskInputReference,
        prompt: TaskInputReference,
        profile: Option<TaskInputReference>,
    },
    #[serde(rename = "video.acquire")]
    VideoAcquire { source: TaskInputReference },
    #[serde(rename = "video.subscription")]
    VideoSubscription { source: TaskInputReference },
    #[serde(rename = "video.transcribe")]
    VideoTranscribe {
        video: TaskInputReference,
        subtitle: Option<TaskInputReference>,
    },
    #[serde(rename = "video.frames")]
    VideoFrames {
        video: TaskInputReference,
        transcript: TaskInputReference,
    },
    #[serde(rename = "video.mechanical_note")]
    VideoMechanicalNote {
        transcript: TaskInputReference,
        frames: TaskInputReference,
    },
    #[serde(rename = "ai.video_note")]
    AiVideoNote {
        transcript: TaskInputReference,
        mechanical_note: TaskInputReference,
        frames: TaskInputReference,
        prompt: TaskInputReference,
        profile: Option<TaskInputReference>,
    },
    #[serde(rename = "core.validate")]
    CoreValidate {
        source: TaskInputReference,
        notes: TaskInputReference,
    },
    #[serde(rename = "core.publish")]
    CorePublish { validated: TaskInputReference },
}

impl TaskInputBindings {
    #[must_use]
    pub const fn executor(&self) -> Executor {
        match self {
            Self::DocumentAcquire { .. } => Executor::DocumentAcquire,
            Self::DocumentExtract { .. } => Executor::DocumentExtract,
            Self::AiDocumentTranslate { .. } => Executor::AiDocumentTranslate,
            Self::AiDocumentNote { .. } => Executor::AiDocumentNote,
            Self::VideoAcquire { .. } => Executor::VideoAcquire,
            Self::VideoSubscription { .. } => Executor::VideoSubscription,
            Self::VideoTranscribe { .. } => Executor::VideoTranscribe,
            Self::VideoFrames { .. } => Executor::VideoFrames,
            Self::VideoMechanicalNote { .. } => Executor::VideoMechanicalNote,
            Self::AiVideoNote { .. } => Executor::AiVideoNote,
            Self::CoreValidate { .. } => Executor::CoreValidate,
            Self::CorePublish { .. } => Executor::CorePublish,
        }
    }

    #[must_use]
    pub fn is_valid(&self) -> bool {
        use TaskInputReference::{DomainProfile, Need, NeedArtifact, Prompt, Source};

        let artifact = |value: &TaskInputReference| matches!(value, NeedArtifact { .. });
        let profile = |value: &Option<TaskInputReference>| {
            value
                .as_ref()
                .is_none_or(|value| matches!(value, DomainProfile))
        };
        match self {
            Self::DocumentAcquire { source }
            | Self::VideoAcquire { source }
            | Self::VideoSubscription { source } => matches!(source, Source),
            Self::DocumentExtract { pdf } => artifact(pdf),
            Self::AiDocumentTranslate {
                document,
                prompt,
                profile: domain,
            }
            | Self::AiDocumentNote {
                document,
                prompt,
                profile: domain,
            } => artifact(document) && matches!(prompt, Prompt(_)) && profile(domain),
            Self::VideoTranscribe { video, subtitle } => {
                artifact(video) && subtitle.as_ref().is_none_or(artifact)
            }
            Self::VideoFrames { video, transcript } => artifact(video) && artifact(transcript),
            Self::VideoMechanicalNote { transcript, frames } => {
                artifact(transcript) && artifact(frames)
            }
            Self::AiVideoNote {
                transcript,
                mechanical_note,
                frames,
                prompt,
                profile: domain,
            } => {
                artifact(transcript)
                    && artifact(mechanical_note)
                    && artifact(frames)
                    && matches!(prompt, Prompt(_))
                    && profile(domain)
            }
            Self::CoreValidate { source, notes } => artifact(source) && matches!(notes, Need(_)),
            Self::CorePublish { validated } => artifact(validated),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct JobInputs {
    pub translate: bool,
    pub source_kind: SourceKind,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bindings_are_executor_tagged_and_strict() {
        let bindings = TaskInputBindings::DocumentExtract {
            pdf: TaskInputReference::NeedArtifact {
                task: "acquire".into(),
                artifact: "original".into(),
            },
        };
        let json = serde_json::to_string(&bindings).expect("serialize");
        assert!(
            serde_json::from_str::<TaskInputBindings>(&json)
                .expect("strict bindings")
                .is_valid()
        );
        serde_json::from_str::<TaskInputBindings>(
            r#"{"executor":"document.extract","inputs":{"pdf":"source","extra":1}}"#,
        )
        .expect_err("unknown input field");
    }

    #[test]
    fn bindings_reject_wrong_reference_semantics() {
        let bindings = TaskInputBindings::DocumentExtract {
            pdf: TaskInputReference::Source,
        };
        assert!(!bindings.is_valid());
    }
}
