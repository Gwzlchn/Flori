use std::collections::BTreeSet;

use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

use crate::ArtifactId;

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct PdfRect {
    pub x1: f64,
    pub y1: f64,
    pub x2: f64,
    pub y2: f64,
}

impl PdfRect {
    #[must_use]
    pub fn is_valid_on(&self, width_pt: f64, height_pt: f64) -> bool {
        [self.x1, self.y1, self.x2, self.y2, width_pt, height_pt]
            .iter()
            .all(|value| value.is_finite())
            && width_pt > 0.0
            && height_pt > 0.0
            && 0.0 <= self.x1
            && self.x1 < self.x2
            && self.x2 <= width_pt
            && 0.0 <= self.y1
            && self.y1 < self.y2
            && self.y2 <= height_pt
    }

    #[must_use]
    pub fn contains(&self, other: &Self) -> bool {
        self.x1 <= other.x1 && self.y1 <= other.y1 && other.x2 <= self.x2 && other.y2 <= self.y2
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
pub enum DocumentStructureSchema {
    #[serde(rename = "flori.document_structure.v1")]
    V1,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentPage {
    pub page: u32,
    pub width_pt: f64,
    pub height_pt: f64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentSection {
    pub id: String,
    pub heading: String,
    pub blocks: Vec<DocumentTextBlock>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentTextBlock {
    pub page: u32,
    pub bbox: PdfRect,
    pub text: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentFigure {
    pub id: String,
    pub page: u32,
    pub bbox: PdfRect,
    pub caption: String,
    pub artifact_name: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentTable {
    pub id: String,
    pub page: u32,
    pub bbox: PdfRect,
    pub caption: String,
    pub text: String,
    pub artifact_name: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct DocumentStructure {
    pub schema: DocumentStructureSchema,
    pub source_artifact_id: ArtifactId,
    pub language: String,
    pub pages: Vec<DocumentPage>,
    pub sections: Vec<DocumentSection>,
    pub figures: Vec<DocumentFigure>,
    pub tables: Vec<DocumentTable>,
}

impl DocumentStructure {
    pub fn validate(&self) -> Result<(), &'static str> {
        if self.language.is_empty()
            || self.language.len() > 32
            || !self
                .language
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || self.pages.is_empty()
        {
            return Err("invalid document language or pages");
        }
        for (index, page) in self.pages.iter().enumerate() {
            if usize::try_from(page.page).ok() != Some(index + 1)
                || !page.width_pt.is_finite()
                || !page.height_pt.is_finite()
                || page.width_pt <= 0.0
                || page.height_pt <= 0.0
            {
                return Err("pages must be ordered, finite, and positive");
            }
        }
        let mut ids = BTreeSet::new();
        let mut artifact_names = BTreeSet::new();
        let mut previous_page = 0;
        for section in &self.sections {
            if !ids.insert(section.id.as_str())
                || !valid_key(&section.id)
                || section.heading.trim().is_empty()
                || section.blocks.is_empty()
            {
                return Err("invalid document section");
            }
            for block in &section.blocks {
                if block.page < previous_page
                    || block.text.trim().is_empty()
                    || !rect_on_page(&self.pages, block.page, &block.bbox)
                {
                    return Err("invalid document text block");
                }
                previous_page = block.page;
            }
        }
        for figure in &self.figures {
            if !ids.insert(figure.id.as_str())
                || !valid_key(&figure.id)
                || figure.caption.trim().is_empty()
                || !valid_artifact_name(&figure.artifact_name)
                || !artifact_names.insert(figure.artifact_name.as_str())
                || !rect_on_page(&self.pages, figure.page, &figure.bbox)
            {
                return Err("invalid document figure");
            }
        }
        for table in &self.tables {
            if !ids.insert(table.id.as_str())
                || !valid_key(&table.id)
                || table.caption.trim().is_empty()
                || table.text.trim().is_empty()
                || !valid_artifact_name(&table.artifact_name)
                || !artifact_names.insert(table.artifact_name.as_str())
                || !rect_on_page(&self.pages, table.page, &table.bbox)
            {
                return Err("invalid document table");
            }
        }
        Ok(())
    }
}

fn rect_on_page(pages: &[DocumentPage], page: u32, rect: &PdfRect) -> bool {
    page.checked_sub(1)
        .and_then(|index| usize::try_from(index).ok())
        .and_then(|index| pages.get(index))
        .is_some_and(|page| rect.is_valid_on(page.width_pt, page.height_pt))
}

fn valid_key(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
}

fn valid_artifact_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && !value.starts_with('.')
        && !value.contains('\\')
        && value
            .split('/')
            .all(|part| !part.is_empty() && !matches!(part, "." | "..") && !part.starts_with('.'))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn structure() -> DocumentStructure {
        DocumentStructure {
            schema: DocumentStructureSchema::V1,
            source_artifact_id: ArtifactId::generate(),
            language: "en".into(),
            pages: vec![DocumentPage {
                page: 1,
                width_pt: 100.0,
                height_pt: 200.0,
            }],
            sections: vec![DocumentSection {
                id: "section-1".into(),
                heading: "Introduction".into(),
                blocks: vec![DocumentTextBlock {
                    page: 1,
                    bbox: PdfRect {
                        x1: 0.0,
                        y1: 0.0,
                        x2: 100.0,
                        y2: 40.0,
                    },
                    text: "Document text.".into(),
                }],
            }],
            figures: vec![DocumentFigure {
                id: "figure-1".into(),
                page: 1,
                bbox: PdfRect {
                    x1: 1.0,
                    y1: 2.0,
                    x2: 20.0,
                    y2: 30.0,
                },
                caption: "Figure 1".into(),
                artifact_name: "figures/figure-1.png".into(),
            }],
            tables: vec![],
        }
    }

    #[test]
    fn document_structure_is_strict_and_bounded() {
        let document = structure();
        assert_eq!(document.validate(), Ok(()));
        let json = serde_json::to_string(&document).expect("serialize document");
        assert!(json.contains("flori.document_structure.v1"));
        serde_json::from_str::<DocumentStructure>(
            &json.replace("\"language\":\"en\"", "\"language\":\"en\",\"extra\":true"),
        )
        .expect_err("unknown fields must fail");

        let mut invalid = document;
        invalid.pages[0].page = 2;
        assert!(invalid.validate().is_err());
    }
}
