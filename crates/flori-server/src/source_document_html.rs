use std::collections::BTreeSet;

use flori_core::ErrorCode;
use lol_html::{RewriteStrSettings, element, rewrite_str};

const READER_HEAD: &str = r#"<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>html{color:#172033;background:#fff;font:16px/1.72 system-ui,sans-serif}body{max-width:920px;margin:auto;padding:32px}img,table{max-width:100%}a{color:#315fbd}pre{white-space:pre-wrap}</style>"#;

pub(super) fn render(html: &str, resources: &BTreeSet<&str>) -> Result<String, ErrorCode> {
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
            .append_element_content_handler(element!("img", |item| {
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
            .append_element_content_handler(element!("head", |item| {
                item.append(READER_HEAD, lol_html::html_content::ContentType::Html);
                Ok(())
            })),
    )
    .map_err(|_| ErrorCode::CorruptState)
}
