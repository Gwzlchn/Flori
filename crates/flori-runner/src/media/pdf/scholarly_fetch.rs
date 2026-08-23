use std::time::Duration;

use flori_core::{ErrorCode, ScholarlyProvider, Sha256Digest};
use reqwest::{StatusCode, Url, header};
use sha2::{Digest, Sha256};

use super::network;

pub(super) async fn fetch(
    mut url: Url,
    max_bytes: u64,
    timeout: Duration,
) -> Result<(Vec<u8>, Url, String), ErrorCode> {
    for redirects in 0..=network::MAX_REDIRECTS {
        let client = network::pinned_client(&url, timeout).await?;
        let mut response = client
            .get(url.clone())
            .header(header::USER_AGENT, "Flori/1 scholarly reader")
            .send()
            .await
            .map_err(|_| ErrorCode::NetworkTemporary)?;
        if response.status().is_redirection() {
            if redirects == network::MAX_REDIRECTS {
                return Err(ErrorCode::UnsupportedSource);
            }
            let location = response
                .headers()
                .get(header::LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or(ErrorCode::UnsupportedSource)?;
            url = network::parse_http_url(
                url.join(location)
                    .map_err(|_| ErrorCode::UnsupportedSource)?
                    .as_str(),
            )?;
            continue;
        }
        if response.status() != StatusCode::OK {
            return Err(if response.status() == StatusCode::TOO_MANY_REQUESTS {
                ErrorCode::UpstreamRateLimited
            } else {
                ErrorCode::UnsupportedSource
            });
        }
        let media = response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.split(';').next())
            .map(str::trim)
            .ok_or(ErrorCode::UnsupportedSource)?
            .to_owned();
        let mut body = Vec::new();
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| ErrorCode::NetworkTemporary)?
        {
            let next = body
                .len()
                .checked_add(chunk.len())
                .filter(|size| u64::try_from(*size).is_ok_and(|size| size <= max_bytes))
                .ok_or(ErrorCode::ArtifactTooLarge)?;
            body.reserve(next - body.len());
            body.extend_from_slice(&chunk);
        }
        if body.is_empty() {
            return Err(ErrorCode::UnsupportedSource);
        }
        return Ok((body, url, media));
    }
    Err(ErrorCode::UnsupportedSource)
}

pub(super) fn provider_url(provider: ScholarlyProvider, url: &Url) -> bool {
    let host = url.host_str().unwrap_or_default();
    match provider {
        ScholarlyProvider::Arxiv => matches!(host, "arxiv.org" | "www.arxiv.org"),
        ScholarlyProvider::Ar5iv => {
            matches!(host, "ar5iv.labs.arxiv.org" | "ar5iv.org" | "www.ar5iv.org")
        }
    }
}

pub(super) fn digest(bytes: &[u8]) -> Result<Sha256Digest, ErrorCode> {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| ErrorCode::Internal)
}
