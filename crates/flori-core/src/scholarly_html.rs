use std::collections::BTreeSet;

use lol_html::{RewriteStrSettings, element, rewrite_str};

pub fn sanitize_scholarly_html(
    html: &str,
    resources: &BTreeSet<&str>,
    head: Option<&'static str>,
) -> Result<String, &'static str> {
    rewrite_str(
        html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!(
                "script,iframe,frame,object,embed,form,input,button,textarea,select,option,link,style,base,meta,svg,canvas,audio,video,source",
                |item| {
                    item.remove();
                    Ok(())
                }
            ))
            .append_element_content_handler(element!("img", move |item| {
                let valid = item
                    .get_attribute("data-flori-resource")
                    .is_some_and(|name| resources.contains(name.as_str()));
                item.remove_attribute("src");
                if !valid {
                    item.remove();
                }
                Ok(())
            }))
            .append_element_content_handler(element!("*", |item| {
                let names = item
                    .attributes()
                    .iter()
                    .map(|attribute| attribute.name())
                    .collect::<Vec<_>>();
                for name in names {
                    let lower = name.to_ascii_lowercase();
                    if lower.starts_with("on")
                        || matches!(
                            lower.as_str(),
                            "style" | "srcset" | "srcdoc" | "action" | "formaction" | "nonce"
                                | "integrity" | "crossorigin" | "referrerpolicy" | "xlink:href"
                        )
                    {
                        item.remove_attribute(&name);
                    }
                }
                Ok(())
            }))
            .append_element_content_handler(element!("a[href]", |item| {
                if item
                    .get_attribute("href")
                    .is_some_and(|href| !href.starts_with('#'))
                {
                    item.remove_attribute("href");
                }
                Ok(())
            }))
            .append_element_content_handler(element!("head", move |item| {
                if let Some(head) = head {
                    item.append(head, lol_html::html_content::ContentType::Html);
                }
                Ok(())
            })),
    )
    .map_err(|_| "invalid scholarly HTML")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_closes_active_and_uncommitted_content() {
        let html = r#"<html><head></head><body><script>x</script><a href="https://evil.example">leave</a><img src="x" data-flori-resource="scholarly_resources/ok.png"><img data-flori-resource="scholarly_resources/missing.png"></body></html>"#;
        let output = sanitize_scholarly_html(
            html,
            &BTreeSet::from(["scholarly_resources/ok.png"]),
            Some("<style>body{color:black}</style>"),
        )
        .expect("sanitize");
        assert!(output.contains("scholarly_resources/ok.png"));
        assert!(output.contains("body{color:black}"));
        for forbidden in ["<script", "evil.example", "missing.png", " src="] {
            assert!(!output.contains(forbidden), "{forbidden}");
        }
    }
}
