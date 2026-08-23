use std::{path::Path, time::Duration};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ErrorCode, JobId, ResolvedSource,
    SCHOLARLY_HTML_MAX_BYTES, ScholarlyFile, ScholarlyHtmlSnapshot, ScholarlyHtmlSnapshotSchema,
    ScholarlyProvider, SourceKind, TaskClaim,
};
use lol_html::{RewriteStrSettings, element, rewrite_str};
use tokio::fs;

use super::{claim, network, scholarly_fetch, upload};
use crate::RunnerClient;

pub(super) async fn capture(
    client: &RunnerClient,
    task: &TaskClaim,
    source: &ResolvedSource,
    root: &Path,
    timeout: Duration,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    if source.kind != SourceKind::Arxiv {
        return Ok(vec![]);
    }
    let id = source
        .canonical_ref
        .strip_prefix("arxiv:")
        .filter(|value| !value.is_empty())
        .ok_or(ErrorCode::CorruptState)?;
    for (provider, url) in [
        (
            ScholarlyProvider::Arxiv,
            format!("https://arxiv.org/html/{id}"),
        ),
        (
            ScholarlyProvider::Ar5iv,
            format!("https://ar5iv.labs.arxiv.org/html/{id}"),
        ),
    ] {
        if let Ok(captured) = capture_provider(task.job_id, provider, &url, timeout).await {
            persist(root, captured).await?;
            return Ok(vec![
                upload::file(
                    client,
                    task,
                    claim::exact(task, ArtifactKind::ScholarlyHtml)?,
                    "scholarly_html".into(),
                    "text/html",
                    &root.join("document.html"),
                )
                .await?,
                upload::file(
                    client,
                    task,
                    claim::exact(task, ArtifactKind::ScholarlyHtmlSnapshot)?,
                    "scholarly_snapshot".into(),
                    "application/json",
                    &root.join("snapshot.json"),
                )
                .await?,
            ]);
        }
    }
    Ok(vec![])
}

struct Captured {
    html: String,
    snapshot: ScholarlyHtmlSnapshot,
}

async fn capture_provider(
    job_id: JobId,
    provider: ScholarlyProvider,
    url: &str,
    timeout: Duration,
) -> Result<Captured, ErrorCode> {
    let requested = network::parse_http_url(url)?;
    let (bytes, final_url, media) =
        scholarly_fetch::fetch(requested, SCHOLARLY_HTML_MAX_BYTES, timeout).await?;
    if media != "text/html" || !scholarly_fetch::provider_url(provider, &final_url) {
        return Err(ErrorCode::UnsupportedSource);
    }
    let source = String::from_utf8(bytes).map_err(|_| ErrorCode::UnsupportedSource)?;
    let lower = source.to_ascii_lowercase();
    if !lower.contains("ltx_document") || !lower.contains("</body>") || !lower.contains("</html>") {
        return Err(ErrorCode::UnsupportedSource);
    }
    let html = sanitize(&source)?;
    let snapshot = ScholarlyHtmlSnapshot {
        schema: ScholarlyHtmlSnapshotSchema::V1,
        job_id,
        provider,
        document_url: final_url.to_string(),
        html: ScholarlyFile {
            artifact_name: "scholarly_html".into(),
            media_type: "text/html".into(),
            size_bytes: u64::try_from(html.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?,
            sha256: scholarly_fetch::digest(html.as_bytes())?,
        },
        stylesheets: vec![],
        resources: vec![],
    };
    snapshot.validate().map_err(|_| ErrorCode::CorruptState)?;
    Ok(Captured { html, snapshot })
}

async fn persist(root: &Path, captured: Captured) -> Result<(), ErrorCode> {
    fs::create_dir_all(root)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    fs::write(root.join("document.html"), captured.html)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let snapshot_bytes = serde_json::to_vec(&captured.snapshot).map_err(|_| ErrorCode::Internal)?;
    if snapshot_bytes.len() > 1024 * 1024 {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    fs::write(root.join("snapshot.json"), snapshot_bytes)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    Ok(())
}

fn sanitize(html: &str) -> Result<String, ErrorCode> {
    rewrite_str(
        html,
        RewriteStrSettings::new()
            .append_element_content_handler(element!(
                "script,iframe,frame,object,embed,form,input,button,textarea,select,option,link,style,base,meta,svg,canvas,audio,video,source,img",
                |element| {
                    element.remove();
                    Ok(())
                }
            ))
            .append_element_content_handler(element!("*", |element| {
                let names = element
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
                        element.remove_attribute(&name);
                    }
                }
                Ok(())
            }))
            .append_element_content_handler(element!("a[href]", |element| {
                if element
                    .get_attribute("href")
                    .is_some_and(|href| !href.starts_with('#'))
                {
                    element.remove_attribute("href");
                }
                Ok(())
            })),
    )
    .map_err(|_| ErrorCode::UnsupportedSource)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitizer_removes_active_and_remote_content() {
        let input = r##"<!doctype html><html><body class="ltx_document"><script>alert(1)</script><form><input></form><a href="https://evil.example">leave</a><img src="figure.png" onerror="steal()" style="width:1px"></body></html>"##;
        let output = sanitize(input).expect("sanitize");
        for forbidden in [
            "<script",
            "<form",
            "<img",
            "onerror",
            "style=",
            "evil.example",
        ] {
            assert!(!output.contains(forbidden));
        }
    }
}
