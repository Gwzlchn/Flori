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
        if !safe_url(&url) {
            return Err(ErrorCode::UnsupportedSource);
        }
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
    if !safe_url(url) {
        return false;
    }
    let host = url.host_str().unwrap_or_default();
    match provider {
        ScholarlyProvider::Arxiv => matches!(host, "arxiv.org" | "www.arxiv.org"),
        ScholarlyProvider::Ar5iv => {
            matches!(host, "ar5iv.labs.arxiv.org" | "ar5iv.org" | "www.ar5iv.org")
        }
    }
}

fn safe_url(url: &Url) -> bool {
    url.scheme() == "https"
        && url.host_str().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.fragment().is_none()
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

pub(super) fn image_extension(media: &str, bytes: &[u8]) -> Result<&'static str, ErrorCode> {
    match media {
        "image/png" if bytes.starts_with(b"\x89PNG\r\n\x1a\n") => Ok("png"),
        "image/jpeg" if bytes.starts_with(b"\xff\xd8\xff") => Ok("jpg"),
        "image/gif" if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") => Ok("gif"),
        "image/webp"
            if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP".as_slice()) =>
        {
            Ok("webp")
        }
        "image/avif"
            if bytes.get(4..8) == Some(b"ftyp".as_slice())
                && matches!(bytes.get(8..12), Some(b"avif") | Some(b"avis")) =>
        {
            Ok("avif")
        }
        _ => Err(ErrorCode::UnsupportedSource),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn image_media_must_match_magic() {
        for (media, bytes, extension) in [
            ("image/png", b"\x89PNG\r\n\x1a\n".as_slice(), "png"),
            ("image/jpeg", b"\xff\xd8\xffx".as_slice(), "jpg"),
            ("image/gif", b"GIF89ax".as_slice(), "gif"),
            ("image/webp", b"RIFFxxxxWEBP".as_slice(), "webp"),
            ("image/avif", b"xxxxftypavif".as_slice(), "avif"),
        ] {
            assert_eq!(image_extension(media, bytes), Ok(extension));
        }
        assert_eq!(
            image_extension("image/png", b"<svg></svg>"),
            Err(ErrorCode::UnsupportedSource)
        );
        assert_eq!(
            image_extension("image/svg+xml", b"<svg></svg>"),
            Err(ErrorCode::UnsupportedSource)
        );
    }

    #[test]
    fn provider_urls_are_https_without_credentials_or_fragments() {
        for value in [
            "http://arxiv.org/html/1",
            "https://user@arxiv.org/html/1",
            "https://arxiv.org/html/1#fragment",
            "https://evil.example/html/1",
        ] {
            assert!(!provider_url(
                ScholarlyProvider::Arxiv,
                &Url::parse(value).expect("URL")
            ));
        }
        assert!(provider_url(
            ScholarlyProvider::Arxiv,
            &Url::parse("https://arxiv.org/html/1").expect("URL")
        ));
    }
}
