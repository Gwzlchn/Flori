use std::{collections::BTreeSet, str::FromStr};

use serde::{Deserialize, Serialize};
use utoipa::openapi::RefOr;
use utoipa::openapi::schema::{AdditionalProperties, ObjectBuilder, OneOfBuilder, Schema, Type};
use utoipa::{PartialSchema, ToSchema};

use crate::{ArtifactId, ErrorCode, EvidenceId, PdfRect, TermsManifest, VideoKeyframe};

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
pub enum EvidenceManifestSchema {
    #[serde(rename = "flori.evidence.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(
    tag = "kind",
    content = "value",
    rename_all = "snake_case",
    deny_unknown_fields
)]
pub enum EvidenceLocator {
    Pdf {
        page: u32,
        bbox: PdfRect,
    },
    Video {
        start_ms: u64,
        end_ms: u64,
        #[serde(skip_serializing_if = "Option::is_none")]
        keyframe: Option<VideoKeyframe>,
    },
}

impl PartialSchema for EvidenceLocator {
    fn schema() -> RefOr<Schema> {
        let closed = || Some(AdditionalProperties::FreeForm(false));
        let pdf_value = ObjectBuilder::new()
            .property("page", u32::schema())
            .property("bbox", PdfRect::schema())
            .required("page")
            .required("bbox")
            .additional_properties(closed());
        let pdf = ObjectBuilder::new()
            .property(
                "kind",
                ObjectBuilder::new()
                    .schema_type(Type::String)
                    .enum_values(Some(["pdf"])),
            )
            .property("value", pdf_value)
            .required("kind")
            .required("value")
            .additional_properties(closed());
        let video_value = ObjectBuilder::new()
            .property("start_ms", u64::schema())
            .property("end_ms", u64::schema())
            .property("keyframe", VideoKeyframe::schema())
            .required("start_ms")
            .required("end_ms")
            .additional_properties(closed());
        let video = ObjectBuilder::new()
            .property(
                "kind",
                ObjectBuilder::new()
                    .schema_type(Type::String)
                    .enum_values(Some(["video"])),
            )
            .property("value", video_value)
            .required("kind")
            .required("value")
            .additional_properties(closed());
        OneOfBuilder::new().item(pdf).item(video).into()
    }
}

impl ToSchema for EvidenceLocator {
    fn schemas(schemas: &mut Vec<(String, RefOr<Schema>)>) {
        schemas.push((PdfRect::name().into(), PdfRect::schema()));
        schemas.push((VideoKeyframe::name().into(), VideoKeyframe::schema()));
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceEntry {
    pub evidence_id: EvidenceId,
    pub source_artifact_id: ArtifactId,
    pub locator: EvidenceLocator,
    pub quote: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct EvidenceManifest {
    pub schema: EvidenceManifestSchema,
    pub items: Vec<EvidenceEntry>,
}

impl EvidenceManifest {
    pub fn validate_structure(&self) -> Result<(), &'static str> {
        let mut ids = BTreeSet::new();
        for item in &self.items {
            if !ids.insert(item.evidence_id) || item.quote.trim().is_empty() {
                return Err("evidence IDs must be unique and quotes non-empty");
            }
            match &item.locator {
                EvidenceLocator::Pdf { page, bbox } => {
                    if *page == 0
                        || ![bbox.x1, bbox.y1, bbox.x2, bbox.y2]
                            .iter()
                            .all(|value| value.is_finite())
                        || bbox.x1 < 0.0
                        || bbox.y1 < 0.0
                        || bbox.x1 >= bbox.x2
                        || bbox.y1 >= bbox.y2
                    {
                        return Err("invalid PDF evidence locator");
                    }
                }
                EvidenceLocator::Video {
                    start_ms, end_ms, ..
                } if start_ms >= end_ms => {
                    return Err("invalid video evidence locator");
                }
                EvidenceLocator::Video { .. } => {}
            }
        }
        Ok(())
    }
}

pub(crate) fn referenced_evidence(
    text: &str,
    allowed: &BTreeSet<EvidenceId>,
) -> Result<BTreeSet<EvidenceId>, crate::ErrorCode> {
    let mut result = BTreeSet::new();
    let mut remaining = text;
    while let Some(start) = remaining.find("[[evidence:") {
        remaining = &remaining[start + 11..];
        let end = remaining
            .find("]]")
            .ok_or(crate::ErrorCode::EvidenceInvalid)?;
        let id = EvidenceId::from_str(&remaining[..end])
            .map_err(|_| crate::ErrorCode::EvidenceInvalid)?;
        if !allowed.contains(&id) {
            return Err(crate::ErrorCode::EvidenceInvalid);
        }
        result.insert(id);
        remaining = &remaining[end + 2..];
    }
    Ok(result)
}

pub(crate) fn normalize(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn smart_note_sections(markdown: &str) -> Option<(&str, &str)> {
    let fact = markdown.find("## 来源事实")? + "## 来源事实".len();
    let analysis_heading = markdown.find("## AI 分析")?;
    if fact >= analysis_heading {
        return None;
    }
    let analysis = analysis_heading + "## AI 分析".len();
    let fact_body = markdown[fact..analysis_heading].trim();
    let analysis_body = markdown[analysis..]
        .split("\n## ")
        .next()
        .unwrap_or_default()
        .trim();
    (!fact_body.is_empty() && !analysis_body.is_empty()).then_some((fact_body, analysis_body))
}

pub(crate) fn validate_note_outputs(
    terms: &TermsManifest,
    smart_note: &str,
    summary: &str,
) -> Result<EvidenceManifest, ErrorCode> {
    let Some((source_facts, _analysis)) = smart_note_sections(smart_note) else {
        return Err(ErrorCode::EvidenceInvalid);
    };
    if terms.evidence_candidates.is_empty() || terms.terms.is_empty() {
        return Err(ErrorCode::EvidenceInvalid);
    }
    let manifest = EvidenceManifest {
        schema: EvidenceManifestSchema::V1,
        items: terms.evidence_candidates.clone(),
    };
    manifest
        .validate_structure()
        .map_err(|_| ErrorCode::EvidenceInvalid)?;
    let ids = manifest
        .items
        .iter()
        .map(|item| item.evidence_id)
        .collect::<BTreeSet<_>>();
    let mut used = referenced_evidence(source_facts, &ids)?;
    let summary_ids = referenced_evidence(summary, &ids)?;
    if used.is_empty() || summary_ids.is_empty() {
        return Err(ErrorCode::EvidenceInvalid);
    }
    used.extend(summary_ids);
    let mut normalized_terms = BTreeSet::new();
    for term in &terms.terms {
        if normalize(&term.term).is_empty()
            || normalize(&term.explanation).is_empty()
            || !normalized_terms.insert(normalize(&term.term))
            || term.evidence_ids.is_empty()
        {
            return Err(ErrorCode::EvidenceInvalid);
        }
        let mut term_ids = BTreeSet::new();
        for id in &term.evidence_ids {
            if !ids.contains(id) || !term_ids.insert(*id) {
                return Err(ErrorCode::EvidenceInvalid);
            }
            used.insert(*id);
        }
    }
    (used == ids)
        .then_some(manifest)
        .ok_or(ErrorCode::EvidenceInvalid)
}
