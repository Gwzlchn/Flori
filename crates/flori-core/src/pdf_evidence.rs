use crate::evidence::{normalize, validate_note_outputs};
use crate::{
    DocumentStructure, ErrorCode, EvidenceLocator, EvidenceManifest, PdfRect, TermsManifest,
};

pub fn validate_pdf_evidence(
    document: &DocumentStructure,
    terms: &TermsManifest,
    smart_note: &str,
    summary: &str,
) -> Result<EvidenceManifest, ErrorCode> {
    document
        .validate()
        .map_err(|_| ErrorCode::EvidenceInvalid)?;
    let manifest = validate_note_outputs(terms, smart_note, summary)?;
    for item in &manifest.items {
        let EvidenceLocator::Pdf { page, bbox } = &item.locator else {
            return Err(ErrorCode::EvidenceInvalid);
        };
        let Some(bounds) = document.pages.get((*page as usize).saturating_sub(1)) else {
            return Err(ErrorCode::EvidenceInvalid);
        };
        if item.source_artifact_id != document.source_artifact_id
            || !bbox.is_valid_on(bounds.width_pt, bounds.height_pt)
            || !quote_matches(document, *page, bbox, &item.quote)
        {
            return Err(ErrorCode::EvidenceInvalid);
        }
    }
    Ok(manifest)
}

fn quote_matches(document: &DocumentStructure, page: u32, bbox: &PdfRect, quote: &str) -> bool {
    let quote = normalize(quote);
    !quote.is_empty()
        && document
            .sections
            .iter()
            .flat_map(|section| &section.blocks)
            .filter(|block| block.page == page && block.bbox.contains(bbox))
            .map(|block| block.text.as_str())
            .chain(
                document
                    .figures
                    .iter()
                    .filter(|figure| figure.page == page && figure.bbox == *bbox)
                    .map(|figure| figure.caption.as_str()),
            )
            .chain(
                document
                    .tables
                    .iter()
                    .filter(|table| table.page == page && table.bbox == *bbox)
                    .flat_map(|table| [table.caption.as_str(), table.text.as_str()]),
            )
            .any(|source| normalize(source).contains(&quote))
}
