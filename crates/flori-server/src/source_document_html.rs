use std::{cell::RefCell, collections::BTreeSet, rc::Rc};

use flori_core::{ErrorCode, HtmlPdfCrosswalk, HtmlPdfCrosswalkStatus};
use lol_html::{RewriteStrSettings, element, end_tag, rewrite_str, text};

const READER_HEAD: &str = r#"<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>html{color:#172033;background:#fff;font:16px/1.72 system-ui,sans-serif}body{max-width:920px;margin:auto;padding:32px}img,table{max-width:100%}a{color:#315fbd}pre{white-space:pre-wrap}</style>"#;
const BLOCKS: &str =
    "p[id],figcaption[id],td[id],th[id],li[id],h1[id],h2[id],h3[id],h4[id],h5[id],h6[id]";

pub(super) fn render(html: &str, resources: &BTreeSet<&str>) -> Result<String, ErrorCode> {
    flori_core::sanitize_scholarly_html(html, resources, READER_HEAD)
        .map_err(|_| ErrorCode::CorruptState)
}

struct Block {
    id: String,
    text: String,
    closed: bool,
}

pub(super) fn crosswalk(
    evidence_id: flori_core::EvidenceId,
    html: &str,
    quote: &str,
) -> Result<HtmlPdfCrosswalk, ErrorCode> {
    let active = Rc::new(RefCell::new(None::<usize>));
    let blocks = Rc::new(RefCell::new(Vec::<Block>::new()));
    let starts = (active.clone(), blocks.clone());
    let bodies = (active.clone(), blocks.clone());
    rewrite_str(
        html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!(BLOCKS, move |item| {
                let Some(id) = item.get_attribute("id").filter(|id| valid_anchor(id)) else {
                    return Ok(());
                };
                let index = starts.1.borrow().len();
                starts.1.borrow_mut().push(Block {
                    id,
                    text: String::new(),
                    closed: false,
                });
                *starts.0.borrow_mut() = Some(index);
                let end = starts.clone();
                item.on_end_tag(end_tag!(move |_| {
                    if let Some(block) = end.1.borrow_mut().get_mut(index) {
                        block.closed = true;
                    }
                    if *end.0.borrow() == Some(index) {
                        *end.0.borrow_mut() = None;
                    }
                    Ok(())
                }))?;
                Ok(())
            }))
            .append_element_content_handler(text!(BLOCKS, move |text| {
                if let Some(index) = *bodies.0.borrow()
                    && let Some(block) = bodies.1.borrow_mut().get_mut(index)
                {
                    block.text.push_str(text.as_str());
                }
                Ok(())
            })),
    )
    .map_err(|_| ErrorCode::CorruptState)?;
    let quote = normalize(quote);
    let matches = blocks
        .borrow()
        .iter()
        .filter(|block| block.closed && normalize(&block.text).contains(&quote))
        .map(|block| block.id.clone())
        .collect::<BTreeSet<_>>();
    let (status, html_anchor) = match matches.len() {
        0 if document_text(html)?.contains(&quote) => (HtmlPdfCrosswalkStatus::AnchorMissing, None),
        0 => (HtmlPdfCrosswalkStatus::QuoteMissing, None),
        1 => (HtmlPdfCrosswalkStatus::Verified, matches.into_iter().next()),
        _ => (HtmlPdfCrosswalkStatus::QuoteAmbiguous, None),
    };
    Ok(HtmlPdfCrosswalk {
        evidence_id,
        status,
        html_anchor,
    })
}

fn document_text(html: &str) -> Result<String, ErrorCode> {
    let output = Rc::new(RefCell::new(String::new()));
    let text = output.clone();
    rewrite_str(
        html,
        RewriteStrSettings::new().append_element_content_handler(text!("body", move |chunk| {
            text.borrow_mut().push_str(chunk.as_str());
            Ok(())
        })),
    )
    .map_err(|_| ErrorCode::CorruptState)?;
    let normalized = normalize(&output.borrow());
    Ok(normalized)
}

fn normalize(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}

fn valid_anchor(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 256
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b':' | b'.'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn render_removes_active_and_uncommitted_content() {
        let input = r##"<html><head><style>bad</style></head><body><script>x</script><a href="https://evil.example">leave</a><img src="https://evil.example/x" data-flori-resource="scholarly_resources/ok.png"><img data-flori-resource="scholarly_resources/missing.png"></body></html>"##;
        let resources = BTreeSet::from(["scholarly_resources/ok.png"]);
        let output = render(input, &resources).expect("render");
        assert!(output.contains(READER_HEAD));
        assert!(output.contains("data-flori-resource=\"scholarly_resources/ok.png\""));
        for forbidden in [
            "<script",
            "bad</style>",
            "evil.example",
            "missing.png",
            " src=",
        ] {
            assert!(!output.contains(forbidden), "{forbidden}");
        }
    }

    #[test]
    fn crosswalk_only_accepts_one_closed_safe_anchor() {
        let evidence_id = flori_core::EvidenceId::generate();
        for (html, quote, status, anchor) in [
            (
                "<body><p id='s1'>Unique quote.</p></body>",
                "Unique quote.",
                HtmlPdfCrosswalkStatus::Verified,
                Some("s1"),
            ),
            (
                "<body><p id='a'>same</p><p id='b'>same</p></body>",
                "same",
                HtmlPdfCrosswalkStatus::QuoteAmbiguous,
                None,
            ),
            (
                "<body><div>body only</div></body>",
                "body only",
                HtmlPdfCrosswalkStatus::AnchorMissing,
                None,
            ),
            (
                "<body><p id='s1'>other</p></body>",
                "missing",
                HtmlPdfCrosswalkStatus::QuoteMissing,
                None,
            ),
        ] {
            let result = crosswalk(evidence_id, html, quote).expect("crosswalk");
            assert_eq!(result.status, status);
            assert_eq!(result.html_anchor.as_deref(), anchor);
        }
    }
}
