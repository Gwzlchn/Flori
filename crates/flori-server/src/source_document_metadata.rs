use flori_core::{ArtifactView, DocumentMetadataView, DocumentStructure, SourceView};

pub(super) fn metadata(
    source: &SourceView,
    pdf: &ArtifactView,
    structure: &DocumentStructure,
) -> DocumentMetadataView {
    let arxiv = source.canonical_ref.strip_prefix("arxiv:");
    let (arxiv_id, arxiv_version) = arxiv.map_or((None, None), |value| {
        let version = value
            .rsplit_once('v')
            .and_then(|(_, version)| version.parse().ok());
        (Some(value.to_owned()), version)
    });
    let abstract_text = structure.sections.iter().find_map(|section| {
        section
            .heading
            .eq_ignore_ascii_case("abstract")
            .then(|| {
                section
                    .blocks
                    .iter()
                    .map(|block| block.text.as_str())
                    .collect::<Vec<_>>()
                    .join("\n")
            })
            .filter(|text| text.len() <= 16 * 1024)
    });
    DocumentMetadataView {
        title: source.title.clone(),
        authors: Vec::new(),
        abstract_text,
        published_at_ms: None,
        language: structure.language.clone(),
        page_count: structure.pages.len() as u32,
        arxiv_id,
        arxiv_version,
        original_url: arxiv
            .map(|id| format!("https://arxiv.org/abs/{id}"))
            .or_else(|| {
                source
                    .canonical_ref
                    .strip_prefix("url:")
                    .map(ToOwned::to_owned)
            }),
        original_size_bytes: pdf.size_bytes,
    }
}
