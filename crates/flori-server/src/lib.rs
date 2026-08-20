//! Home Core HTTP 边界。

#![forbid(unsafe_code)]

mod error;
mod protocol;
mod runner;
mod runner_upload;

use std::sync::Arc;

use axum::Router;
use flori_core::ErrorCode;
use flori_store::{Store, artifact::NasArtifactStore};

pub fn app(
    store: Arc<Store>,
    artifacts: Arc<NasArtifactStore>,
    artifact_download_base: String,
    lease_ms: u64,
) -> Result<Router, ErrorCode> {
    runner::routes(store, artifacts, artifact_download_base, lease_ms)
}

#[cfg(test)]
mod tests {
    use std::{fs, path::PathBuf};

    use flori_core::{AttemptId, CreateRunnerSlot, ErrorResponse, Sha256Digest};
    use sha2::{Digest, Sha256};
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
    };

    use super::*;

    #[tokio::test]
    async fn runner_http_is_strict_over_real_tcp_and_sqlite() {
        let root = temporary_root();
        fs::create_dir_all(&root).expect("create test root");
        let store = Arc::new(Store::open(root.join("flori.sqlite")).await.expect("store"));
        let artifacts = Arc::new(
            NasArtifactStore::new(root.join("artifacts"), 16 * 1024 * 1024)
                .expect("artifact store"),
        );
        store
            .create_runner_slot(
                &CreateRunnerSlot {
                    name: "test-runner".to_owned(),
                    tags: vec!["media".to_owned()],
                    max_concurrency: 1,
                    default_model: None,
                    default_effort: None,
                },
                &digest("registration-token"),
                4_000_000_000_000,
                1,
            )
            .await
            .expect("runner slot");

        let listener = TcpListener::bind("localhost:0").await.expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                app(
                    store,
                    artifacts,
                    "http://localhost/artifacts".to_owned(),
                    60_000,
                )
                .expect("test config"),
            )
            .await
            .expect("serve");
        });

        assert_error(
            &exchange(address, request("/runner/v1/poll", &[], "")).await,
            400,
            ErrorCode::ProtocolMismatch,
        );
        assert_error(
            &exchange(
                address,
                request(
                    "/runner/v1/poll",
                    &["X-Flori-Protocol: 1", "X-Flori-Protocol: 1"],
                    "",
                ),
            )
            .await,
            400,
            ErrorCode::ProtocolMismatch,
        );
        let unknown = request(
            "/runner/v1/register",
            &[
                "X-Flori-Protocol: 1",
                "Authorization: Bearer registration-token",
                "Content-Type: application/json",
            ],
            r#"{"tools":[],"ai_models":[],"extra":true}"#,
        );
        assert_error(
            &exchange(address, unknown).await,
            400,
            ErrorCode::InvalidRequest,
        );

        let registered = exchange(
            address,
            request(
                "/runner/v1/register",
                &[
                    "X-Flori-Protocol: 1",
                    "Authorization: Bearer registration-token",
                    "Content-Type: application/json",
                ],
                r#"{"tools":[],"ai_models":[]}"#,
            ),
        )
        .await;
        assert_eq!(status(&registered), 200);
        let registered: flori_core::RegisterRunnerResponse =
            serde_json::from_slice(body(&registered)).expect("registration response");
        let authorization = format!("Authorization: Bearer {}", registered.token);

        let poll = exchange(
            address,
            request(
                "/runner/v1/poll",
                &["X-Flori-Protocol: 1", &authorization],
                "",
            ),
        )
        .await;
        assert_eq!(status(&poll), 204);

        let exec_id = AttemptId::generate();
        assert_error(
            &exchange(
                address,
                request(
                    &format!("/runner/v1/attempts/{exec_id}/renew"),
                    &["X-Flori-Protocol: 1", &authorization],
                    "",
                ),
            )
            .await,
            409,
            ErrorCode::StaleAttempt,
        );
        let log = concat!(
            r#"{"sequence":1,"sha256":"2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b","line":"secret"}"#,
            "\n"
        );
        assert_error(
            &exchange(
                address,
                request(
                    &format!("/runner/v1/attempts/{exec_id}/logs"),
                    &[
                        "X-Flori-Protocol: 1",
                        &authorization,
                        "Content-Type: application/x-ndjson",
                    ],
                    log,
                ),
            )
            .await,
            409,
            ErrorCode::StaleAttempt,
        );
        assert_error(
            &exchange(
                address,
                request(
                    &format!("/runner/v1/attempts/{exec_id}/usage"),
                    &[
                        "X-Flori-Protocol: 1",
                        &authorization,
                        "Content-Type: application/json",
                    ],
                    r#"{"state":"started","invocation_key":"one","tool":"qoder_cli","model":"m","effort":"high"}"#,
                ),
            )
            .await,
            409,
            ErrorCode::StaleAttempt,
        );

        server.abort();
        let _ = server.await;
        fs::remove_dir_all(root).expect("remove test root");
    }

    fn temporary_root() -> PathBuf {
        std::env::temp_dir().join(format!(
            "flori-server-{}",
            flori_core::RequestId::generate()
        ))
    }

    fn digest(value: &str) -> Sha256Digest {
        let encoded = Sha256::digest(value.as_bytes())
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        Sha256Digest::parse(encoded).expect("digest")
    }

    fn request(path: &str, headers: &[&str], body: &str) -> String {
        let mut request = format!(
            "POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {}\r\n",
            body.len()
        );
        for header in headers {
            request.push_str(header);
            request.push_str("\r\n");
        }
        request.push_str("\r\n");
        request.push_str(body);
        request
    }

    async fn exchange(address: std::net::SocketAddr, request: String) -> Vec<u8> {
        let mut stream = TcpStream::connect(address).await.expect("connect");
        stream
            .write_all(request.as_bytes())
            .await
            .expect("write request");
        let mut response = Vec::new();
        stream
            .read_to_end(&mut response)
            .await
            .expect("read response");
        response
    }

    fn status(response: &[u8]) -> u16 {
        let line = response
            .split(|byte| *byte == b'\n')
            .next()
            .expect("status line");
        std::str::from_utf8(line)
            .expect("UTF-8 status")
            .split_whitespace()
            .nth(1)
            .expect("status code")
            .parse()
            .expect("numeric status")
    }

    fn body(response: &[u8]) -> &[u8] {
        response
            .windows(4)
            .position(|window| window == b"\r\n\r\n")
            .map(|index| &response[index + 4..])
            .expect("HTTP body")
    }

    fn assert_error(response: &[u8], expected_status: u16, expected_code: ErrorCode) {
        assert_eq!(status(response), expected_status);
        let error: ErrorResponse = serde_json::from_slice(body(response)).expect("error response");
        assert_eq!(error.error.code, expected_code);
    }
}
