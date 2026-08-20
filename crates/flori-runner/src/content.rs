use std::path::Path;

use flori_core::{ErrorCode, ResolvedArtifact, ResolvedSourceInput, Sha256Digest};
use reqwest::{StatusCode, Url, header};
use sha2::{Digest, Sha256};
use tokio::{
    fs::OpenOptions,
    io::{AsyncWriteExt, BufWriter},
};

use crate::{ClientError, RunnerClient};

const CHUNK_BYTES: u64 = 1024 * 1024;
const NETWORK_ATTEMPTS: usize = 3;

impl RunnerClient {
    pub async fn download_artifact(
        &self,
        input: &ResolvedArtifact,
        destination: &Path,
    ) -> Result<(), ClientError> {
        let expected =
            self.content_url(&format!("api/v1/artifacts/{}/content", input.artifact_id))?;
        self.download(
            &input.download_url,
            expected,
            &input.media_type,
            input.size_bytes,
            &input.sha256,
            destination,
        )
        .await
    }

    pub async fn download_source_input(
        &self,
        input: &ResolvedSourceInput,
        destination: &Path,
    ) -> Result<(), ClientError> {
        let expected = self.content_url(&format!(
            "api/v1/source-inputs/{}/content",
            input.source_input_id
        ))?;
        self.download(
            &input.download_url,
            expected,
            &input.media_type,
            input.size_bytes,
            &input.sha256,
            destination,
        )
        .await
    }

    async fn download(
        &self,
        declared_url: &str,
        expected_url: Url,
        media_type: &str,
        size_bytes: u64,
        sha256: &Sha256Digest,
        destination: &Path,
    ) -> Result<(), ClientError> {
        let declared = Url::parse(declared_url).map_err(|_| invalid())?;
        if declared != expected_url || destination.parent().is_none() {
            return Err(invalid());
        }
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(destination)
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        let mut file = BufWriter::new(file);
        let mut hasher = Sha256::new();
        let mut offset = 0_u64;
        while offset < size_bytes {
            let end = offset
                .saturating_add(CHUNK_BYTES)
                .min(size_bytes)
                .saturating_sub(1);
            let bytes = self
                .range(&declared, media_type, sha256, offset, end, size_bytes)
                .await?;
            file.write_all(&bytes)
                .await
                .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
            file.flush()
                .await
                .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
            file.get_ref()
                .sync_data()
                .await
                .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
            hasher.update(&bytes);
            offset = end + 1;
        }
        file.flush()
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        file.get_ref()
            .sync_all()
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        if offset != size_bytes || !digest_is(&hasher.finalize(), sha256) {
            return Err(ClientError::local(ErrorCode::DigestMismatch));
        }
        Ok(())
    }

    async fn range(
        &self,
        url: &Url,
        media_type: &str,
        sha256: &Sha256Digest,
        start: u64,
        end: u64,
        total: u64,
    ) -> Result<Vec<u8>, ClientError> {
        let expected_len = end - start + 1;
        for attempt in 0..NETWORK_ATTEMPTS {
            let response = self
                .send(
                    self.content_request(url.clone())
                        .header(header::RANGE, format!("bytes={start}-{end}")),
                )
                .await;
            let mut response = match response {
                Ok(response) => response,
                Err(error) if error.code() == ErrorCode::NetworkTemporary => {
                    if attempt + 1 == NETWORK_ATTEMPTS {
                        return Err(error);
                    }
                    continue;
                }
                Err(error) => return Err(error),
            };
            validate_headers(&response, media_type, sha256, start, end, total)?;
            let mut bytes = Vec::with_capacity(
                usize::try_from(expected_len)
                    .map_err(|_| ClientError::local(ErrorCode::ArtifactTooLarge))?,
            );
            loop {
                match response.chunk().await {
                    Ok(Some(chunk)) => {
                        if bytes.len().saturating_add(chunk.len()) > bytes.capacity() {
                            return Err(ClientError::local(ErrorCode::CorruptState));
                        }
                        bytes.extend_from_slice(&chunk);
                    }
                    Ok(None) if bytes.len() == bytes.capacity() => return Ok(bytes),
                    Ok(None) => break,
                    Err(_) => break,
                }
            }
            if attempt + 1 == NETWORK_ATTEMPTS {
                return Err(ClientError::local(ErrorCode::NetworkTemporary));
            }
        }
        Err(ClientError::local(ErrorCode::NetworkTemporary))
    }
}

fn validate_headers(
    response: &reqwest::Response,
    media_type: &str,
    sha256: &Sha256Digest,
    start: u64,
    end: u64,
    total: u64,
) -> Result<(), ClientError> {
    let headers = response.headers();
    let len = end - start + 1;
    if response.status() != StatusCode::PARTIAL_CONTENT
        || header_text(headers, header::ACCEPT_RANGES)? != "bytes"
        || header_text(headers, header::CONTENT_RANGE)? != format!("bytes {start}-{end}/{total}")
        || header_text(headers, header::CONTENT_LENGTH)? != len.to_string()
        || header_text(headers, header::CONTENT_TYPE)? != media_type
        || header_text(headers, header::ETAG)? != format!("\"{}\"", sha256.as_str())
        || headers.contains_key(header::CONTENT_ENCODING)
    {
        return Err(ClientError::local(ErrorCode::CorruptState));
    }
    Ok(())
}

fn header_text(
    headers: &reqwest::header::HeaderMap,
    name: reqwest::header::HeaderName,
) -> Result<&str, ClientError> {
    if headers.get_all(&name).iter().count() != 1 {
        return Err(ClientError::local(ErrorCode::CorruptState));
    }
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| ClientError::local(ErrorCode::CorruptState))
}

fn digest_is(actual: &[u8], expected: &Sha256Digest) -> bool {
    actual
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        == expected.as_str()
}

fn invalid() -> ClientError {
    ClientError::local(ErrorCode::InvalidRequest)
}

#[cfg(test)]
mod tests {
    use std::{fs, sync::Arc};

    use flori_core::{ArtifactId, ArtifactKind};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::TcpListener,
    };

    use super::*;
    use crate::digest;

    #[tokio::test]
    async fn resumes_only_from_a_fully_verified_range() {
        let listener = TcpListener::bind("localhost:0").await.expect("listener");
        let address = listener.local_addr().expect("address");
        let mut body = vec![b'a'; CHUNK_BYTES as usize];
        body.extend_from_slice(b"tail!");
        let digest = digest::sha256(&body).expect("digest");
        let digest_for_server = digest.clone();
        let body = Arc::new(body);
        let server_body = body.clone();
        let server = tokio::spawn(async move {
            for (expected, bytes, truncated) in [
                (
                    "bytes=0-1048575",
                    &server_body[..CHUNK_BYTES as usize],
                    false,
                ),
                (
                    "bytes=1048576-1048580",
                    &server_body[CHUNK_BYTES as usize..],
                    true,
                ),
                (
                    "bytes=1048576-1048580",
                    &server_body[CHUNK_BYTES as usize..],
                    false,
                ),
            ] {
                serve_range(
                    &listener,
                    expected,
                    bytes,
                    1_048_581,
                    &digest_for_server,
                    truncated,
                )
                .await;
            }
        });
        let base = format!("http://{address}");
        let client = RunnerClient::new(&base, "token").expect("client");
        let id = ArtifactId::generate();
        let input = ResolvedArtifact {
            artifact_id: id,
            name: "input".into(),
            kind: ArtifactKind::DocumentStructure,
            media_type: "application/octet-stream".into(),
            size_bytes: body.len() as u64,
            sha256: digest,
            download_url: format!("{base}/api/v1/artifacts/{id}/content"),
        };
        let output = temporary("resume");
        client
            .download_artifact(&input, &output)
            .await
            .expect("download");
        assert_eq!(fs::read(&output).expect("output"), body.as_slice());
        server.await.expect("server");
        fs::remove_file(output).expect("cleanup");
    }

    #[tokio::test]
    async fn rejects_url_and_digest_drift() {
        let client = RunnerClient::new("http://localhost:9", "token").expect("client");
        let id = ArtifactId::generate();
        let expected = digest::sha256(b"hello").expect("digest");
        let mut input = ResolvedArtifact {
            artifact_id: id,
            name: "input".into(),
            kind: ArtifactKind::DocumentStructure,
            media_type: "application/octet-stream".into(),
            size_bytes: 5,
            sha256: expected.clone(),
            download_url: format!("http://other.invalid/api/v1/artifacts/{id}/content"),
        };
        let output = temporary("host");
        assert_eq!(
            client
                .download_artifact(&input, &output)
                .await
                .expect_err("host drift")
                .code(),
            ErrorCode::InvalidRequest
        );
        assert!(!output.exists());

        let listener = TcpListener::bind("localhost:0").await.expect("listener");
        let address = listener.local_addr().expect("address");
        let server_digest = expected.clone();
        let server = tokio::spawn(async move {
            serve_range(&listener, "bytes=0-4", b"world", 5, &server_digest, false).await;
        });
        let base = format!("http://{address}");
        let client = RunnerClient::new(&base, "token").expect("client");
        input.download_url = format!("{base}/api/v1/artifacts/{id}/content");
        let output = temporary("digest");
        assert_eq!(
            client
                .download_artifact(&input, &output)
                .await
                .expect_err("digest drift")
                .code(),
            ErrorCode::DigestMismatch
        );
        server.await.expect("server");
        fs::remove_file(output).expect("cleanup");
    }

    async fn serve_range(
        listener: &TcpListener,
        expected_range: &str,
        bytes: &[u8],
        total: u64,
        digest: &Sha256Digest,
        truncated: bool,
    ) {
        let (mut stream, _) = listener.accept().await.expect("accept");
        let mut request = Vec::new();
        while !request.ends_with(b"\r\n\r\n") {
            let mut chunk = [0_u8; 1024];
            let read = stream.read(&mut chunk).await.expect("request");
            assert_ne!(read, 0);
            request.extend_from_slice(&chunk[..read]);
        }
        let request = std::str::from_utf8(&request)
            .expect("request UTF-8")
            .to_ascii_lowercase();
        assert!(request.contains(&format!(
            "range: {}\r\n",
            expected_range.to_ascii_lowercase()
        )));
        assert!(request.contains("authorization: bearer token\r\n"));
        let (start, end) = expected_range
            .strip_prefix("bytes=")
            .expect("range")
            .split_once('-')
            .expect("bounds");
        let response = format!(
            "HTTP/1.1 206 Partial Content\r\nAccept-Ranges: bytes\r\n\
             Content-Range: bytes {start}-{end}/{total}\r\nContent-Length: {}\r\n\
             Content-Type: application/octet-stream\r\nETag: \"{}\"\r\n\
             Connection: close\r\n\r\n",
            bytes.len(),
            digest.as_str()
        );
        stream
            .write_all(response.as_bytes())
            .await
            .expect("headers");
        let sent = if truncated { &bytes[..2] } else { bytes };
        stream.write_all(sent).await.expect("body");
    }

    fn temporary(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "flori-content-{name}-{}",
            flori_core::RequestId::generate()
        ))
    }
}
