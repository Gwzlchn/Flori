use std::{cell::RefCell, rc::Rc};

use flori_core::{ArtifactView, DocumentMetadataView, DocumentStructure, ErrorCode, SourceView};
use lol_html::{RewriteStrSettings, element, end_tag, rewrite_str, text};

pub(super) fn metadata(
    source: &SourceView,
    pdf: &ArtifactView,
    structure: &DocumentStructure,
    html: Option<&str>,
) -> Result<DocumentMetadataView, ErrorCode> {
    let arxiv = source.canonical_ref.strip_prefix("arxiv:");
    let (arxiv_id, arxiv_version) = arxiv.map_or((None, None), |value| {
        let versioned = value
            .rsplit_once('v')
            .and_then(|(id, version)| version.parse().ok().map(|version| (id, version)));
        versioned.map_or_else(
            || (Some(value.to_owned()), None),
            |(id, version)| (Some(id.to_owned()), Some(version)),
        )
    });
    let fallback_abstract = structure.sections.iter().find_map(|section| {
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
    let mut metadata = DocumentMetadataView {
        title: source.title.clone(),
        authors: Vec::new(),
        abstract_text: fallback_abstract,
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
    };
    if let Some(html) = html {
        apply_html(&mut metadata, html)?;
    }
    Ok(metadata)
}

fn apply_html(metadata: &mut DocumentMetadataView, html: &str) -> Result<(), ErrorCode> {
    let title = Rc::new(RefCell::new(String::new()));
    let abstract_text = Rc::new(RefCell::new(String::new()));
    let authors = Rc::new(RefCell::new(Vec::<String>::new()));
    let active_author = Rc::new(RefCell::new(None::<usize>));
    let author_start = (authors.clone(), active_author.clone());
    let author_text = (authors.clone(), active_author.clone());
    let title_text = title.clone();
    let abstract_body = abstract_text.clone();
    rewrite_str(
        html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!(".ltx_personname,.author", move |item| {
                let index = author_start.0.borrow().len();
                author_start.0.borrow_mut().push(String::new());
                *author_start.1.borrow_mut() = Some(index);
                let end = author_start.1.clone();
                item.on_end_tag(end_tag!(move |_| {
                    *end.borrow_mut() = None;
                    Ok(())
                }))?;
                Ok(())
            }))
            .append_element_content_handler(text!(".ltx_personname,.author", move |chunk| {
                if let Some(index) = *author_text.1.borrow()
                    && let Some(author) = author_text.0.borrow_mut().get_mut(index)
                {
                    author.push_str(chunk.as_str());
                }
                Ok(())
            }))
            .append_element_content_handler(text!("h1.ltx_title,h1.title", move |chunk| {
                title_text.borrow_mut().push_str(chunk.as_str());
                Ok(())
            }))
            .append_element_content_handler(text!(
                ".ltx_abstract,blockquote.abstract",
                move |chunk| {
                    abstract_body.borrow_mut().push_str(chunk.as_str());
                    Ok(())
                }
            )),
    )
    .map_err(|_| ErrorCode::CorruptState)?;
    let bounded = |value: &str, max| {
        let value = normalize(value);
        (!value.is_empty() && value.len() <= max).then_some(value)
    };
    let mut authors = authors
        .borrow()
        .iter()
        .filter_map(|value| bounded(value, 256))
        .collect::<Vec<_>>();
    authors.sort();
    authors.dedup();
    if authors.len() > 64 {
        return Err(ErrorCode::CorruptState);
    }
    metadata.title = bounded(&title.borrow(), 512).or_else(|| metadata.title.take());
    metadata.authors = authors;
    metadata.abstract_text =
        bounded(&abstract_text.borrow(), 16 * 1024).or_else(|| metadata.abstract_text.take());
    Ok(())
}

fn normalize(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_bounded_scholarly_metadata() {
        let html = "<h1 class='ltx_title'>Attention Is All You Need</h1><div class='ltx_authors'><span class='ltx_personname'>Alice A.</span><span class='ltx_personname'>Bob B.</span></div><div class='ltx_abstract'><p>A useful abstract.</p></div>";
        let mut metadata = DocumentMetadataView {
            title: None,
            authors: vec![],
            abstract_text: None,
            published_at_ms: None,
            language: "en".into(),
            page_count: 1,
            arxiv_id: None,
            arxiv_version: None,
            original_url: None,
            original_size_bytes: 1,
        };
        apply_html(&mut metadata, html).expect("metadata");
        assert_eq!(metadata.title.as_deref(), Some("Attention Is All You Need"));
        assert_eq!(metadata.authors, ["Alice A.", "Bob B."]);
        assert_eq!(
            metadata.abstract_text.as_deref(),
            Some("A useful abstract.")
        );
    }

    #[test]
    fn separates_arxiv_id_from_version() {
        let source = SourceView {
            source_id: "018f0000-0000-7000-8000-000000000001"
                .parse()
                .expect("source ID"),
            domain_id: "018f0000-0000-7000-8000-000000000002"
                .parse()
                .expect("domain ID"),
            collection_ids: vec![],
            kind: flori_core::SourceKind::Arxiv,
            canonical_ref: "arxiv:1706.03762v2".into(),
            title: None,
            current_job_id: None,
            previous_job_id: None,
        };
        let structure = DocumentStructure {
            schema: flori_core::DocumentStructureSchema::V1,
            source_artifact_id: "018f0000-0000-7000-8000-000000000003"
                .parse()
                .expect("artifact ID"),
            language: "en".into(),
            pages: vec![],
            sections: vec![],
            figures: vec![],
            tables: vec![],
        };
        let pdf = ArtifactView {
            artifact_id: structure.source_artifact_id,
            job_id: "018f0000-0000-7000-8000-000000000004"
                .parse()
                .expect("job ID"),
            task_id: "018f0000-0000-7000-8000-000000000005"
                .parse()
                .expect("task ID"),
            source_id: source.source_id,
            name: "source_original".into(),
            kind: flori_core::ArtifactKind::SourceOriginal,
            media_type: "application/pdf".into(),
            size_bytes: 1,
            sha256: flori_core::Sha256Digest::parse(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )
            .expect("digest"),
        };

        let metadata = metadata(&source, &pdf, &structure, None).expect("metadata");
        assert_eq!(metadata.arxiv_id.as_deref(), Some("1706.03762"));
        assert_eq!(metadata.arxiv_version, Some(2));
        assert_eq!(
            metadata.original_url.as_deref(),
            Some("https://arxiv.org/abs/1706.03762v2")
        );
    }
}
