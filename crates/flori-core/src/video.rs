use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::{ArtifactId, SourceKind};

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct VideoKeyframe {
    pub artifact_id: ArtifactId,
    pub timestamp_ms: u64,
}

impl VideoKeyframe {
    pub fn from_artifact_name(
        artifact_id: ArtifactId,
        artifact_name: &str,
    ) -> Result<Self, &'static str> {
        let timestamp = artifact_name
            .strip_prefix("frames/")
            .and_then(|name| name.strip_suffix(".jpg"))
            .filter(|value| value.len() == 13 && value.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or("invalid keyframe Artifact name")?;
        Ok(Self {
            artifact_id,
            timestamp_ms: timestamp,
        })
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
pub enum TranscriptSchema {
    #[serde(rename = "flori.transcript.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TranscriptCue {
    pub start_ms: u64,
    pub end_ms: u64,
    pub text: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TranscriptManifest {
    pub schema: TranscriptSchema,
    pub source_artifact_id: ArtifactId,
    pub language: String,
    pub duration_ms: u64,
    pub cues: Vec<TranscriptCue>,
}

impl TranscriptManifest {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.duration_ms == 0 || self.language.trim().is_empty() {
            return Err("invalid transcript metadata");
        }
        let mut previous_end = 0;
        for cue in &self.cues {
            if cue.start_ms < previous_end
                || cue.start_ms >= cue.end_ms
                || cue.end_ms > self.duration_ms
                || cue.text.trim().is_empty()
            {
                return Err("invalid transcript cue");
            }
            previous_end = cue.end_ms;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
pub enum PartsManifestSchema {
    #[serde(rename = "flori.parts_manifest.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct VideoPart {
    pub index: u16,
    pub title: String,
    pub duration_ms: u64,
    pub video_artifact_name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub subtitle_artifact_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub danmaku_artifact_name: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PartsManifest {
    pub schema: PartsManifestSchema,
    pub parts: Vec<VideoPart>,
}

impl PartsManifest {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.parts.is_empty() {
            return Err("parts must not be empty");
        }
        for (index, part) in self.parts.iter().enumerate() {
            if usize::from(part.index) != index + 1
                || part.title.trim().is_empty()
                || part.duration_ms == 0
                || !valid_name(&part.video_artifact_name)
                || !part
                    .subtitle_artifact_name
                    .as_deref()
                    .is_none_or(valid_name)
                || !part.danmaku_artifact_name.as_deref().is_none_or(valid_name)
            {
                return Err("invalid video part");
            }
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
pub enum SubscriptionManifestSchema {
    #[serde(rename = "flori.subscription_manifest.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionItem {
    pub kind: SourceKind,
    pub canonical_ref: String,
    pub title: String,
    pub published_at_ms: i64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionManifest {
    pub schema: SubscriptionManifestSchema,
    pub items: Vec<SubscriptionItem>,
}

impl SubscriptionManifest {
    pub fn validate(&self, fanout_limit: u16) -> Result<(), &'static str> {
        if fanout_limit == 0 || self.items.len() > usize::from(fanout_limit) {
            return Err("subscription fanout exceeded");
        }
        let mut seen = HashSet::new();
        let mut previous_time = i64::MAX;
        for item in &self.items {
            if !matches!(
                item.kind,
                SourceKind::BilibiliVideo | SourceKind::YoutubeVideo
            ) || item.canonical_ref.trim().is_empty()
                || item.title.trim().is_empty()
                || item.published_at_ms < 0
                || item.published_at_ms > previous_time
                || !seen.insert((item.kind, item.canonical_ref.as_str()))
            {
                return Err("invalid subscription item");
            }
            previous_time = item.published_at_ms;
        }
        Ok(())
    }
}

fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && !value.starts_with('/')
        && !value.contains('\\')
        && !value.starts_with('.')
        && value
            .split('/')
            .all(|part| !part.is_empty() && !matches!(part, "." | "..") && !part.starts_with('.'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn transcript_rejects_overlap_and_out_of_range_cues() {
        let mut transcript = TranscriptManifest {
            schema: TranscriptSchema::V1,
            source_artifact_id: ArtifactId::generate(),
            language: "en".into(),
            duration_ms: 2_000,
            cues: vec![
                TranscriptCue {
                    start_ms: 0,
                    end_ms: 1_000,
                    text: "one".into(),
                },
                TranscriptCue {
                    start_ms: 1_000,
                    end_ms: 2_000,
                    text: "two".into(),
                },
            ],
        };
        assert_eq!(transcript.validate(), Ok(()));
        transcript.cues[1].start_ms = 999;
        assert!(transcript.validate().is_err());
    }

    #[test]
    fn manifests_enforce_order_and_platform_scope() {
        let parts = PartsManifest {
            schema: PartsManifestSchema::V1,
            parts: vec![VideoPart {
                index: 1,
                title: "part".into(),
                duration_ms: 1,
                video_artifact_name: "videos/part-1.mp4".into(),
                subtitle_artifact_name: None,
                danmaku_artifact_name: None,
            }],
        };
        assert_eq!(parts.validate(), Ok(()));
        assert_eq!(
            VideoKeyframe::from_artifact_name(ArtifactId::generate(), "frames/0000000001000.jpg")
                .expect("keyframe name")
                .timestamp_ms,
            1_000
        );
        assert!(
            VideoKeyframe::from_artifact_name(ArtifactId::generate(), "frames/1000.jpg").is_err()
        );

        let subscriptions = SubscriptionManifest {
            schema: SubscriptionManifestSchema::V1,
            items: vec![SubscriptionItem {
                kind: SourceKind::YoutubeVideo,
                canonical_ref: "youtube:item".into(),
                title: "item".into(),
                published_at_ms: 1,
            }],
        };
        assert_eq!(subscriptions.validate(1), Ok(()));
        assert!(subscriptions.validate(0).is_err());
    }
}
