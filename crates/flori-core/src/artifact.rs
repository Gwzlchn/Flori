use std::collections::BTreeSet;

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
            Self::ScholarlyHtml => media_type == "text/html",
            Self::ScholarlyHtmlSnapshot => media_type == "application/json",
            Self::ScholarlyResource => matches!(
                media_type,
                "text/css"
                    | "font/woff"
                    | "font/woff2"
                    | "font/ttf"
                    | "font/otf"
                    | "application/font-woff"
                    | "application/x-font-ttf"
                    | "application/x-font-opentype"
                    | "image/avif"
                    | "image/gif"
                    | "image/jpeg"
                    | "image/png"
                    | "image/webp"
            ),
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

pub const SCHOLARLY_HTML_MAX_BYTES: u64 = 32 * 1024 * 1024;
pub const SCHOLARLY_RESOURCE_MAX_BYTES: u64 = 8 * 1024 * 1024;
pub const SCHOLARLY_RESOURCE_TOTAL_MAX_BYTES: u64 = 32 * 1024 * 1024;
pub const SCHOLARLY_MAX_RESOURCES: usize = 256;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
pub enum ScholarlyHtmlSnapshotSchema {
    #[serde(rename = "flori.scholarly_html_snapshot.v1")]
    V1,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ScholarlyProvider {
    Arxiv,
    Ar5iv,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ScholarlyResourceKind {
    Stylesheet,
    Image,
    Font,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ScholarlyFile {
    pub artifact_name: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ScholarlyResource {
    pub artifact_name: String,
    pub kind: ScholarlyResourceKind,
    pub request_url: String,
    pub source_url: String,
    pub media_type: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct ScholarlyHtmlSnapshot {
    pub schema: ScholarlyHtmlSnapshotSchema,
    pub job_id: JobId,
    pub provider: ScholarlyProvider,
    pub document_url: String,
    pub html: ScholarlyFile,
    pub stylesheets: Vec<String>,
    pub resources: Vec<ScholarlyResource>,
}

impl ScholarlyHtmlSnapshot {
    pub fn validate(&self) -> Result<(), &'static str> {
        if !valid_document_url(self.provider, &self.document_url)
            || self.html.artifact_name != "scholarly_html"
            || self.html.media_type != "text/html"
            || self.html.size_bytes == 0
            || self.html.size_bytes > SCHOLARLY_HTML_MAX_BYTES
            || self.resources.len() > SCHOLARLY_MAX_RESOURCES
            || self.stylesheets.len() > 32
        {
            return Err("invalid scholarly HTML snapshot");
        }
        let mut names = BTreeSet::new();
        let mut urls = BTreeSet::new();
        let mut total_bytes = 0_u64;
        let mut previous_name = "";
        for resource in &self.resources {
            if !valid_scholarly_name(&resource.artifact_name)
                || resource.artifact_name.as_str() <= previous_name
                || !valid_https_url(&resource.request_url)
                || !valid_https_url(&resource.source_url)
                || resource.size_bytes == 0
                || resource.size_bytes > SCHOLARLY_RESOURCE_MAX_BYTES
                || !resource_media_matches(resource.kind, &resource.media_type)
                || !names.insert(resource.artifact_name.as_str())
            {
                return Err("invalid scholarly resource");
            }
            let aliases = [resource.request_url.as_str(), resource.source_url.as_str()]
                .into_iter()
                .collect::<BTreeSet<_>>();
            if aliases.iter().any(|url| urls.contains(url)) {
                return Err("duplicate scholarly resource URL");
            }
            urls.extend(aliases);
            total_bytes = total_bytes
                .checked_add(resource.size_bytes)
                .ok_or("scholarly resource size overflow")?;
            if total_bytes > SCHOLARLY_RESOURCE_TOTAL_MAX_BYTES {
                return Err("scholarly resource total exceeds limit");
            }
            previous_name = &resource.artifact_name;
        }
        let mut stylesheet_names = BTreeSet::new();
        for name in &self.stylesheets {
            if !stylesheet_names.insert(name.as_str())
                || !self.resources.iter().any(|resource| {
                    resource.artifact_name == *name
                        && resource.kind == ScholarlyResourceKind::Stylesheet
                })
            {
                return Err("invalid scholarly stylesheet reference");
            }
        }
        Ok(())
    }
}

fn valid_document_url(provider: ScholarlyProvider, value: &str) -> bool {
    let prefixes: &[&str] = match provider {
        ScholarlyProvider::Arxiv => &["https://arxiv.org/html/", "https://www.arxiv.org/html/"],
        ScholarlyProvider::Ar5iv => &[
            "https://ar5iv.labs.arxiv.org/html/",
            "https://ar5iv.org/html/",
            "https://www.ar5iv.org/html/",
        ],
    };
    prefixes.iter().any(|prefix| value.starts_with(prefix)) && valid_https_url(value)
}

fn valid_https_url(value: &str) -> bool {
    value
        .strip_prefix("https://")
        .and_then(|rest| rest.split(['/', '?']).next())
        .is_some_and(|authority| !authority.is_empty())
        && value.len() <= 8192
        && !value.contains(['@', '#', '\\'])
        && !value.chars().any(char::is_control)
}

fn valid_scholarly_name(value: &str) -> bool {
    value
        .strip_prefix("scholarly_resources/")
        .is_some_and(|name| {
            !name.is_empty()
                && name.len() <= 255
                && !name.starts_with('.')
                && !name.contains(['/', '\\', '\0'])
        })
}

fn resource_media_matches(kind: ScholarlyResourceKind, media_type: &str) -> bool {
    match kind {
        ScholarlyResourceKind::Stylesheet => media_type == "text/css",
        ScholarlyResourceKind::Image => matches!(
            media_type,
            "image/avif" | "image/gif" | "image/jpeg" | "image/png" | "image/webp"
        ),
        ScholarlyResourceKind::Font => matches!(
            media_type,
            "font/woff"
                | "font/woff2"
                | "font/ttf"
                | "font/otf"
                | "application/font-woff"
                | "application/x-font-ttf"
                | "application/x-font-opentype"
        ),
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
            (ArtifactKind::ScholarlyHtml, "text/html"),
            (ArtifactKind::ScholarlyHtmlSnapshot, "application/json"),
            (ArtifactKind::ScholarlyResource, "text/css"),
        ] {
            assert!(kind.accepts_media_type(media_type));
            if kind != ArtifactKind::ScholarlyHtml {
                assert!(!kind.accepts_media_type("text/html"));
            }
        }
    }

    #[test]
    fn scholarly_snapshot_is_strict_and_bounded() {
        let snapshot = ScholarlyHtmlSnapshot {
            schema: ScholarlyHtmlSnapshotSchema::V1,
            job_id: JobId::generate(),
            provider: ScholarlyProvider::Arxiv,
            document_url: "https://arxiv.org/html/1706.03762".into(),
            html: ScholarlyFile {
                artifact_name: "scholarly_html".into(),
                media_type: "text/html".into(),
                size_bytes: 10,
                sha256: Sha256Digest::parse("a".repeat(64)).expect("digest"),
            },
            stylesheets: vec!["scholarly_resources/a.css".into()],
            resources: vec![ScholarlyResource {
                artifact_name: "scholarly_resources/a.css".into(),
                kind: ScholarlyResourceKind::Stylesheet,
                request_url: "https://arxiv.org/html/1706.03762/a.css".into(),
                source_url: "https://arxiv.org/html/1706.03762/a.css?v=1".into(),
                media_type: "text/css".into(),
                size_bytes: 10,
                sha256: Sha256Digest::parse("b".repeat(64)).expect("digest"),
            }],
        };
        assert_eq!(snapshot.validate(), Ok(()));
        let json = serde_json::to_string(&snapshot).expect("serialize snapshot");
        serde_json::from_str::<ScholarlyHtmlSnapshot>(&json.replace(
            "\"provider\":\"arxiv\"",
            "\"provider\":\"arxiv\",\"extra\":true",
        ))
        .expect_err("unknown field must fail");

        let mut invalid = snapshot;
        invalid.resources[0].source_url = "http://127.0.0.1/style.css".into();
        assert!(invalid.validate().is_err());
    }
}
