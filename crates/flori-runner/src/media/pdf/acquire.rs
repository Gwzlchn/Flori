use std::path::{Path, PathBuf};
use std::time::Duration;

use flori_core::{ErrorCode, ResolvedSource, Sha256Digest, SourceKind};
use reqwest::{StatusCode, Url, header};
use sha2::{Digest, Sha256};
use tokio::fs::{self, OpenOptions};
use tokio::io::AsyncWriteExt;

use super::network::{MAX_REDIRECTS, parse_http_url, pinned_client};

type ExpectedPdf<'a> = Option<(u64, &'a Sha256Digest)>;

#[derive(Clone, Debug)]
pub struct PdfAcquireConfig {
    pub max_bytes: u64,
    pub timeout: Duration,
}

pub async fn acquire_pdf(
    source: &ResolvedSource,
    destination: &Path,
    config: &PdfAcquireConfig,
) -> Result<Sha256Digest, ErrorCode> {
    if config.max_bytes == 0 || config.timeout.is_zero() {
        return Err(ErrorCode::InvalidRequest);
    }
    let (url, expected) = source_url(source)?;
    let result = download(url, destination, config, expected).await;
    if result.is_err() {
        let _ = fs::remove_file(destination).await;
    }
    result
}

fn source_url(source: &ResolvedSource) -> Result<(Url, ExpectedPdf<'_>), ErrorCode> {
    match source.kind {
        SourceKind::PdfUpload => {
            let input = source.input.as_ref().ok_or(ErrorCode::CorruptState)?;
            if input.media_type != "application/pdf"
                || input.name.is_empty()
                || input.size_bytes == 0
            {
                return Err(ErrorCode::CorruptState);
            }
            Ok((
                parse_http_url(&input.download_url)?,
                Some((input.size_bytes, &input.sha256)),
            ))
        }
        SourceKind::PdfUrl => {
            if source.input.is_some() {
                return Err(ErrorCode::CorruptState);
            }
            let value = source
                .canonical_ref
                .strip_prefix("url:")
                .ok_or(ErrorCode::CorruptState)?;
            Ok((parse_http_url(value)?, None))
        }
        SourceKind::Arxiv => {
            if source.input.is_some() {
                return Err(ErrorCode::CorruptState);
            }
            let id = source
                .canonical_ref
                .strip_prefix("arxiv:")
                .filter(|value| valid_arxiv_id(value))
                .ok_or(ErrorCode::CorruptState)?;
            Ok((
                parse_http_url(&format!("https://arxiv.org/pdf/{id}"))?,
                None,
            ))
        }
        _ => Err(ErrorCode::UnsupportedSource),
    }
}

async fn download(
    mut url: Url,
    destination: &Path,
    config: &PdfAcquireConfig,
    expected: ExpectedPdf<'_>,
) -> Result<Sha256Digest, ErrorCode> {
    for redirect_count in 0..=MAX_REDIRECTS {
        let client = pinned_client(&url, config.timeout).await?;
        let mut response = client
            .get(url.clone())
            .header(header::ACCEPT, "application/pdf")
            .send()
            .await
            .map_err(|error| network_error(&error))?;
        if response.status().is_redirection() {
            if redirect_count == MAX_REDIRECTS {
                return Err(ErrorCode::UnsupportedSource);
            }
            let location = response
                .headers()
                .get(header::LOCATION)
                .and_then(|value| value.to_str().ok())
                .ok_or(ErrorCode::UnsupportedSource)?;
            url = parse_http_url(
                url.join(location)
                    .map_err(|_| ErrorCode::UnsupportedSource)?
                    .as_str(),
            )?;
            continue;
        }
        status(response.status())?;
        if response
            .content_length()
            .is_some_and(|size| size > config.max_bytes)
        {
            return Err(ErrorCode::ArtifactTooLarge);
        }
        let mut file = create_file(destination).await?;
        let mut digest = Sha256::new();
        let mut size = 0_u64;
        let mut prefix = Vec::<u8>::with_capacity(5);
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|error| network_error(&error))?
        {
            size = size
                .checked_add(u64::try_from(chunk.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?)
                .filter(|size| *size <= config.max_bytes)
                .ok_or(ErrorCode::ArtifactTooLarge)?;
            if prefix.len() < 5 {
                prefix.extend(chunk.iter().take(5 - prefix.len()));
            }
            digest.update(&chunk);
            file.write_all(&chunk)
                .await
                .map_err(|_| ErrorCode::StorageUnavailable)?;
        }
        file.sync_all()
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        if prefix != b"%PDF-" || size == 0 {
            return Err(ErrorCode::UnsupportedSource);
        }
        let actual = Sha256Digest::parse(
            digest
                .finalize()
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>(),
        )
        .map_err(|_| ErrorCode::Internal)?;
        if let Some((expected_size, expected_digest)) = expected
            && (size != expected_size || &actual != expected_digest)
        {
            return Err(ErrorCode::DigestMismatch);
        }
        return Ok(actual);
    }
    Err(ErrorCode::UnsupportedSource)
}

async fn create_file(path: &Path) -> Result<fs::File, ErrorCode> {
    let parent = path.parent().ok_or(ErrorCode::InvalidRequest)?;
    reject_symlink_parent(parent).await?;
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)
}

async fn reject_symlink_parent(path: &Path) -> Result<(), ErrorCode> {
    let mut current = PathBuf::new();
    for part in path.components() {
        current.push(part);
        let metadata = fs::symlink_metadata(&current)
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(ErrorCode::ArtifactInvalidPath);
        }
    }
    Ok(())
}

fn status(value: StatusCode) -> Result<(), ErrorCode> {
    if value.is_success() {
        Ok(())
    } else if value == StatusCode::TOO_MANY_REQUESTS {
        Err(ErrorCode::UpstreamRateLimited)
    } else if value.is_server_error() {
        Err(ErrorCode::NetworkTemporary)
    } else {
        Err(ErrorCode::UnsupportedSource)
    }
}

fn network_error(error: &reqwest::Error) -> ErrorCode {
    if error.is_timeout() {
        ErrorCode::AttemptTimeout
    } else {
        ErrorCode::NetworkTemporary
    }
}

fn valid_arxiv_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && !value.starts_with('/')
        && !value.ends_with('/')
        && !value.contains("..")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'/'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arxiv_ids_are_path_safe() {
        for value in ["1706.03762", "1706.03762v7", "hep-th/9901001"] {
            assert!(valid_arxiv_id(value));
        }
        for value in ["", "../secret", "/1706.03762", "1706.03762?x=1"] {
            assert!(!valid_arxiv_id(value));
        }
    }
}
