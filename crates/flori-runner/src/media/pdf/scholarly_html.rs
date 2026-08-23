use std::collections::{BTreeMap, BTreeSet};

use flori_core::{ErrorCode, ScholarlyProvider};
use lol_html::{RewriteStrSettings, element, rewrite_str};
use reqwest::Url;

use super::scholarly_fetch;

pub(super) fn image_urls(
    html: &str,
    base: &Url,
    provider: ScholarlyProvider,
) -> Result<Vec<Url>, ErrorCode> {
    let mut urls = BTreeSet::new();
    rewrite_str(
        html,
        RewriteStrSettings::new().append_element_content_handler(element!("img[src]", |item| {
            if let Some(url) = item
                .get_attribute("src")
                .and_then(|value| base.join(&value).ok())
                .filter(|url| matches!(url.scheme(), "http" | "https"))
                .filter(|url| scholarly_fetch::provider_url(provider, url))
            {
                urls.insert(url.to_string());
            }
            Ok(())
        })),
    )
    .map_err(|_| ErrorCode::UnsupportedSource)?;
    urls.into_iter()
        .map(|url| Url::parse(&url).map_err(|_| ErrorCode::UnsupportedSource))
        .collect()
}

pub(super) fn sanitize(
    html: &str,
    base: &Url,
    provider: ScholarlyProvider,
    resources: &BTreeMap<String, String>,
) -> Result<String, ErrorCode> {
    let rewritten = rewrite_str(
        html,
        RewriteStrSettings::new().append_element_content_handler(element!("img", |item| {
            let name = item
                .get_attribute("src")
                .and_then(|value| base.join(&value).ok())
                .filter(|url| scholarly_fetch::provider_url(provider, url))
                .and_then(|url| resources.get(url.as_str()));
            if let Some(name) = name {
                item.set_attribute("data-flori-resource", name)?;
            }
            Ok(())
        })),
    )
    .map_err(|_| ErrorCode::UnsupportedSource)?;
    let allowed = resources.values().map(String::as_str).collect();
    flori_core::sanitize_scholarly_html(&rewritten, &allowed, "")
        .map_err(|_| ErrorCode::UnsupportedSource)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_closes_images_and_active_content() {
        let base = Url::parse("https://arxiv.org/html/1706.03762").expect("base");
        let input = r##"<html><body class="ltx_document"><script>x</script><a href="https://evil.example">leave</a><img src="fig/x.png" onerror="x" style="width:1px"><img src="https://evil.example/y.png"></body></html>"##;
        let urls = image_urls(input, &base, ScholarlyProvider::Arxiv).expect("urls");
        assert_eq!(urls[0].as_str(), "https://arxiv.org/html/fig/x.png");
        let resources =
            BTreeMap::from([(urls[0].to_string(), "scholarly_resources/figure.png".into())]);
        let output = sanitize(input, &base, ScholarlyProvider::Arxiv, &resources).expect("html");
        assert!(output.contains("data-flori-resource=\"scholarly_resources/figure.png\""));
        for forbidden in ["<script", "src=", "onerror", "style=", "evil.example"] {
            assert!(!output.contains(forbidden), "{forbidden}");
        }
    }
}
