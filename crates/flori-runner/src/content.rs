use std::path::Path;

use flori_core::{ErrorCode, ResolvedArtifact, ResolvedSourceInput, Sha256Digest};
use reqwest::{StatusCode, Url, header};
use sha2::{Digest, Sha256};
use tokio::{
    fs::OpenOptions,
    io::{AsyncWriteExt, BufWriter},
};

use crate::{ClientError, RunnerClient};

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
        self.stream_to_file(&declared, media_type, size_bytes, sha256, destination)
            .await
    }

    async fn stream_to_file(
        &self,
        url: &Url,
        media_type: &str,
        total: u64,
        sha256: &Sha256Digest,
        destination: &Path,
    ) -> Result<(), ClientError> {
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(destination)
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        let mut file = BufWriter::new(file);
        let mut hasher = Sha256::new();
        let mut offset = 0_u64;
        if total > 0 {
            let end = total - 1;
            for attempt in 0..NETWORK_ATTEMPTS {
                let start = offset;
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
                loop {
                    match response.chunk().await {
                        Ok(Some(chunk)) => {
                            let chunk_len = u64::try_from(chunk.len())
                                .map_err(|_| ClientError::local(ErrorCode::ArtifactTooLarge))?;
                            if chunk_len > total - offset {
                                return Err(ClientError::local(ErrorCode::CorruptState));
                            }
                            file.write_all(&chunk)
                                .await
                                .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
                            hasher.update(&chunk);
                            offset += chunk_len;
                        }
                        Ok(None) if offset == total => break,
                        Ok(None) | Err(_) => break,
                    }
                }
                file.flush()
                    .await
                    .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
                file.get_ref()
                    .sync_data()
                    .await
                    .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
                if offset == total {
                    break;
                }
                if attempt + 1 == NETWORK_ATTEMPTS {
                    return Err(ClientError::local(ErrorCode::NetworkTemporary));
                }
            }
        }
        file.flush()
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        file.get_ref()
            .sync_all()
            .await
            .map_err(|_| ClientError::local(ErrorCode::StorageUnavailable))?;
        if offset != total || !digest_is(&hasher.finalize(), sha256) {
            return Err(ClientError::local(ErrorCode::DigestMismatch));
        }
        Ok(())
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
    async fn streams_a_large_download_with_one_request() {
        let listener = TcpListener::bind("localhost:0").await.expect("listener");
        let address = listener.local_addr().expect("address");
        let mut body = vec![b'a'; 256 * 1024];
        body.extend_from_slice(b"tail!");
        let digest = digest::sha256(&body).expect("digest");
        let digest_for_server = digest.clone();
        let body = Arc::new(body);
        let server_body = body.clone();
        let server = tokio::spawn(async move {
            serve_range(
                &listener,
                "bytes=0-262148",
                &server_body,
                262_149,
                &digest_for_server,
                None,
            )
            .await;
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
        let output = temporary("single-request");
        client
            .download_artifact(&input, &output)
            .await
            .expect("download");
        assert_eq!(fs::read(&output).expect("output"), body.as_slice());
        server.await.expect("server");
        fs::remove_file(output).expect("cleanup");
    }

    #[tokio::test]
    async fn resumes_from_the_fully_written_offset_after_disconnect() {
        let listener = TcpListener::bind("localhost:0").await.expect("listener");
        let address = listener.local_addr().expect("address");
        let body = Arc::new(b"resume-me".to_vec());
        let digest = digest::sha256(&body).expect("digest");
        let server_digest = digest.clone();
        let server_body = body.clone();
        let server = tokio::spawn(async move {
            serve_range(
                &listener,
                "bytes=0-8",
                &server_body,
                9,
                &server_digest,
                Some(4),
            )
            .await;
            serve_range(
                &listener,
                "bytes=4-8",
                &server_body[4..],
                9,
                &server_digest,
                None,
            )
            .await;
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
    async fn stops_after_three_interrupted_requests() {
        let listener = TcpListener::bind("localhost:0").await.expect("listener");
        let address = listener.local_addr().expect("address");
        let body = Arc::new(b"four".to_vec());
        let digest = digest::sha256(&body).expect("digest");
        let server_digest = digest.clone();
        let server_body = body.clone();
        let server = tokio::spawn(async move {
            for (expected, remaining) in [
                ("bytes=0-3", &server_body[0..]),
                ("bytes=1-3", &server_body[1..]),
                ("bytes=2-3", &server_body[2..]),
            ] {
                serve_range(&listener, expected, remaining, 4, &server_digest, Some(1)).await;
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
        let output = temporary("retry-limit");
        assert_eq!(
            client
                .download_artifact(&input, &output)
                .await
                .expect_err("retry limit")
                .code(),
            ErrorCode::NetworkTemporary
        );
        assert_eq!(fs::read(&output).expect("partial output"), b"fou");
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
            serve_range(&listener, "bytes=0-4", b"world", 5, &server_digest, None).await;
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
        sent_bytes: Option<usize>,
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
        let sent = &bytes[..sent_bytes.unwrap_or(bytes.len())];
        stream.write_all(sent).await.expect("body");
    }

    fn temporary(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!(
            "flori-content-{name}-{}",
            flori_core::RequestId::generate()
        ))
    }
}
