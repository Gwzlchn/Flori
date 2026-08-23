use std::{collections::BTreeMap, path::Path, time::Duration};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ErrorCode, JobId, ResolvedSource,
    SCHOLARLY_HTML_MAX_BYTES, SCHOLARLY_MAX_RESOURCES, SCHOLARLY_RESOURCE_MAX_BYTES,
    SCHOLARLY_RESOURCE_TOTAL_MAX_BYTES, ScholarlyFile, ScholarlyHtmlSnapshot,
    ScholarlyHtmlSnapshotSchema, ScholarlyProvider, SourceKind, TaskClaim,
};
use sha2::{Digest, Sha256};
use tokio::fs;

use super::{claim, network, scholarly_fetch, scholarly_html, upload};
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
            persist(root, &captured).await?;
            let mut entries = vec![
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
            ];
            let declaration = claim::exact(task, ArtifactKind::ScholarlyResource)?;
            for resource in &captured.snapshot.resources {
                entries.push(
                    upload::file(
                        client,
                        task,
                        declaration,
                        resource.artifact_name.clone(),
                        &resource.media_type,
                        &root
                            .join("resources")
                            .join(basename(&resource.artifact_name)?),
                    )
                    .await?,
                );
            }
            return Ok(entries);
        }
    }
    Ok(vec![])
}

struct Captured {
    html: String,
    snapshot: ScholarlyHtmlSnapshot,
    files: Vec<Vec<u8>>,
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
    let image_urls = scholarly_html::image_urls(&source, &final_url, provider)?;
    if image_urls.len() > SCHOLARLY_MAX_RESOURCES {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    let mut fetched = Vec::with_capacity(image_urls.len());
    let mut rewrites = BTreeMap::new();
    let mut total = 0_u64;
    for request_url in image_urls {
        let (bytes, source_url, media_type) =
            scholarly_fetch::fetch(request_url.clone(), SCHOLARLY_RESOURCE_MAX_BYTES, timeout)
                .await?;
        if !scholarly_fetch::provider_url(provider, &source_url) {
            return Err(ErrorCode::UnsupportedSource);
        }
        let extension = scholarly_fetch::image_extension(&media_type, &bytes)?;
        total = total
            .checked_add(u64::try_from(bytes.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?)
            .filter(|total| *total <= SCHOLARLY_RESOURCE_TOTAL_MAX_BYTES)
            .ok_or(ErrorCode::ArtifactTooLarge)?;
        let artifact_name = resource_name(&request_url, &bytes, extension);
        rewrites.insert(request_url.to_string(), artifact_name.clone());
        fetched.push((
            ScholarlyFile {
                artifact_name,
                media_type,
                size_bytes: u64::try_from(bytes.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?,
                sha256: crate::digest::sha256(&bytes).map_err(|_| ErrorCode::Internal)?,
            },
            bytes,
        ));
    }
    fetched.sort_by(|left, right| left.0.artifact_name.cmp(&right.0.artifact_name));
    let (resources, files) = fetched.into_iter().unzip();
    let html = scholarly_html::sanitize(&source, &final_url, provider, &rewrites)?;
    let html_bytes = u64::try_from(html.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?;
    if html_bytes > SCHOLARLY_HTML_MAX_BYTES {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    let snapshot = ScholarlyHtmlSnapshot {
        schema: ScholarlyHtmlSnapshotSchema::V1,
        job_id,
        provider,
        document_url: final_url.to_string(),
        html: ScholarlyFile {
            artifact_name: "scholarly_html".into(),
            media_type: "text/html".into(),
            size_bytes: html_bytes,
            sha256: crate::digest::sha256(html.as_bytes()).map_err(|_| ErrorCode::Internal)?,
        },
        resources,
    };
    snapshot.validate().map_err(|_| ErrorCode::CorruptState)?;
    Ok(Captured {
        html,
        snapshot,
        files,
    })
}

async fn persist(root: &Path, captured: &Captured) -> Result<(), ErrorCode> {
    fs::create_dir_all(root)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    fs::write(root.join("document.html"), &captured.html)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let snapshot_bytes = serde_json::to_vec(&captured.snapshot).map_err(|_| ErrorCode::Internal)?;
    if snapshot_bytes.len() > 1024 * 1024 {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    fs::write(root.join("snapshot.json"), snapshot_bytes)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    if !captured.files.is_empty() {
        fs::create_dir(root.join("resources"))
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
    }
    for (resource, bytes) in captured.snapshot.resources.iter().zip(&captured.files) {
        fs::write(
            root.join("resources")
                .join(basename(&resource.artifact_name)?),
            bytes,
        )
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    }
    Ok(())
}

fn resource_name(url: &reqwest::Url, bytes: &[u8], extension: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(url.as_str().as_bytes());
    digest.update([0]);
    digest.update(bytes);
    let name = digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    format!("scholarly_resources/{name}.{extension}")
}

fn basename(name: &str) -> Result<&str, ErrorCode> {
    claim::basename(
        name.strip_prefix("scholarly_resources/")
            .unwrap_or_default(),
    )
}
