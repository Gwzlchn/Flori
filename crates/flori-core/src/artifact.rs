use serde::{Deserialize, Deserializer, Serialize, de};
use utoipa::ToSchema;

use crate::{ArtifactKind, ArtifactWhen, AttemptId, JobId, TaskId};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactDeclaration {
    pub name: String,
    pub kind: ArtifactKind,
    pub path: String,
    pub required: bool,
    pub when: ArtifactWhen,
    pub max_files: Option<u16>,
    pub max_bytes: u64,
}

impl ArtifactKind {
    #[must_use]
    pub fn accepts_media_type(self, media_type: &str) -> bool {
        match self {
            Self::SourceOriginal => {
                media_type == "application/pdf" || media_type.starts_with("video/")
            }
            Self::DocumentStructure
            | Self::PartsManifest
            | Self::SubscriptionManifest
            | Self::Terms
            | Self::Evidence
            | Self::AiAudit => media_type == "application/json",
            Self::Figure | Self::TableRegion | Self::Keyframe => media_type.starts_with("image/"),
            Self::Translation | Self::MechanicalNote | Self::SmartNote | Self::Summary => {
                media_type == "text/markdown"
            }
            Self::Subtitle => matches!(
                media_type,
                "text/vtt" | "text/plain" | "application/x-subrip"
            ),
            Self::Transcript => matches!(media_type, "application/json" | "text/vtt"),
            Self::Danmaku => matches!(
                media_type,
                "application/json" | "application/xml" | "text/xml"
            ),
            Self::TaskLog => media_type == "application/x-ndjson",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
pub enum ArtifactManifestSchema {
    #[serde(rename = "flori.artifact.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactManifest {
    pub schema: ArtifactManifestSchema,
    pub job_id: JobId,
    pub task_id: TaskId,
    pub exec_id: AttemptId,
    pub artifacts: Vec<ArtifactManifestEntry>,
}

impl ArtifactManifest {
    #[must_use]
    pub fn new(
        job_id: JobId,
        task_id: TaskId,
        exec_id: AttemptId,
        artifacts: Vec<ArtifactManifestEntry>,
    ) -> Self {
        Self {
            schema: ArtifactManifestSchema::V1,
            job_id,
            task_id,
            exec_id,
            artifacts,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ArtifactManifestEntry {
    pub name: String,
    pub kind: ArtifactKind,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub relative_path: String,
}

#[derive(Clone, Debug, Eq, Hash, PartialEq, ToSchema)]
#[schema(value_type = String, pattern = "^[0-9a-f]{64}$")]
pub struct Sha256Digest(String);

impl Sha256Digest {
    pub fn parse(value: impl Into<String>) -> Result<Self, &'static str> {
        let value = value.into();
        if value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            Ok(Self(value))
        } else {
            Err("expected 64 lowercase hexadecimal SHA-256 characters")
        }
    }

    #[must_use]
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Serialize for Sha256Digest {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for Sha256Digest {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        Self::parse(String::deserialize(deserializer)?).map_err(de::Error::custom)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest_json(schema: &str, sha256: &str, extra: &str) -> String {
        format!(
            r#"{{"schema":"{schema}","job_id":"{}","task_id":"{}","exec_id":"{}","artifacts":[{{"name":"note","kind":"smart_note","media_type":"text/markdown","size_bytes":3,"sha256":"{sha256}","relative_path":"sources/path"{extra}}}]}}"#,
            JobId::generate(),
            TaskId::generate(),
            AttemptId::generate(),
        )
    }

    #[test]
    fn manifest_round_trips_with_only_the_frozen_schema() {
        let json = manifest_json(
            "flori.artifact.v1",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "",
        );
        let manifest: ArtifactManifest = serde_json::from_str(&json).expect("strict manifest");
        assert_eq!(manifest.schema, ArtifactManifestSchema::V1);
        assert_eq!(serde_json::to_string(&manifest).expect("serialize"), json);
    }

    #[test]
    fn manifest_rejects_schema_digest_and_field_drift() {
        for json in [
            manifest_json("flori.artifact.v0", &"a".repeat(64), ""),
            manifest_json("flori.artifact.v1", &"A".repeat(64), ""),
            manifest_json("flori.artifact.v1", &"a".repeat(64), ",\"extra\":1"),
        ] {
            serde_json::from_str::<ArtifactManifest>(&json).expect_err("must reject drift");
        }
    }

    #[test]
    fn artifact_media_types_are_closed_by_kind() {
        for (kind, media_type) in [
            (ArtifactKind::SourceOriginal, "application/pdf"),
            (ArtifactKind::SourceOriginal, "video/mp4"),
            (ArtifactKind::DocumentStructure, "application/json"),
            (ArtifactKind::Figure, "image/png"),
            (ArtifactKind::TableRegion, "image/webp"),
            (ArtifactKind::Translation, "text/markdown"),
            (ArtifactKind::Subtitle, "text/vtt"),
            (ArtifactKind::Transcript, "application/json"),
            (ArtifactKind::Keyframe, "image/jpeg"),
            (ArtifactKind::Danmaku, "application/xml"),
            (ArtifactKind::PartsManifest, "application/json"),
            (ArtifactKind::SubscriptionManifest, "application/json"),
            (ArtifactKind::MechanicalNote, "text/markdown"),
            (ArtifactKind::SmartNote, "text/markdown"),
            (ArtifactKind::Summary, "text/markdown"),
            (ArtifactKind::Terms, "application/json"),
            (ArtifactKind::Evidence, "application/json"),
            (ArtifactKind::TaskLog, "application/x-ndjson"),
            (ArtifactKind::AiAudit, "application/json"),
        ] {
            assert!(kind.accepts_media_type(media_type));
            assert!(!kind.accepts_media_type("text/html"));
        }
    }
}
