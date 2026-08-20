use std::io::{Read, Write};
use std::net::TcpListener;
use std::thread;

use flori_core::{
    AiTool, AiUsageId, AiUsageState, ArtifactKind, ArtifactManifestEntry, AttemptAck, AttemptId,
    AttemptState, ErrorBody, ErrorCode, ErrorResponse, FailAttemptRequest, JobId, LogFrame,
    RegisterRunnerRequest, RegisterRunnerResponse, RenewLeaseResponse, RequestId, RunnerId,
    Sha256Digest, StartUploadRequest, StartUploadResponse, TaskId, UploadCursor, UploadId,
    UsageAck, UsageUpdate, VerifyUploadRequest, VerifyUploadResponse,
};
use flori_runner::{RunnerClient, manifest_sha256};
use sha2::{Digest, Sha256};

struct MockResponse {
    status: &'static str,
    body: String,
}

fn serve_once(response: MockResponse) -> (String, thread::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind mock HTTP");
    let address = listener.local_addr().expect("mock address");
    let handle = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        let mut request = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let read = stream.read(&mut buffer).expect("read request");
            assert!(read > 0, "request closed before headers");
            request.extend_from_slice(&buffer[..read]);
            if let Some(position) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                break position + 4;
            }
        };
        let headers = String::from_utf8_lossy(&request[..header_end]);
        let content_length = headers
            .lines()
            .find_map(|line| {
                line.to_ascii_lowercase()
                    .strip_prefix("content-length: ")
                    .and_then(|value| value.parse::<usize>().ok())
            })
            .unwrap_or(0);
        while request.len() < header_end + content_length {
            let read = stream.read(&mut buffer).expect("read request body");
            assert!(read > 0, "request closed before body");
            request.extend_from_slice(&buffer[..read]);
        }
        let wire = format!(
            "HTTP/1.1 {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
            response.status,
            response.body.len(),
            response.body,
        );
        stream.write_all(wire.as_bytes()).expect("write response");
        String::from_utf8(request).expect("UTF-8 request")
    });
    (format!("http://{address}"), handle)
}

fn digest(byte: char) -> Sha256Digest {
    Sha256Digest::parse(byte.to_string().repeat(64)).expect("digest")
}

fn content_digest(bytes: &[u8]) -> Sha256Digest {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("content digest")
}

fn artifact(name: &str, byte: char) -> ArtifactManifestEntry {
    ArtifactManifestEntry {
        name: name.to_owned(),
        kind: ArtifactKind::TaskLog,
        media_type: "application/x-ndjson".to_owned(),
        size_bytes: 3,
        sha256: digest(byte),
        relative_path: format!("sources/final/{name}"),
    }
}

#[tokio::test]
async fn register_and_poll_use_protocol_and_bearer_headers() {
    let returned_token = ["long", "lived"].join("-");
    let response = RegisterRunnerResponse {
        runner_id: RunnerId::generate(),
        token: returned_token,
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&response).expect("response JSON"),
    });
    let registered = RunnerClient::register(
        &base_url,
        "one-time",
        &RegisterRunnerRequest {
            tools: Vec::new(),
            ai_models: Vec::new(),
        },
    )
    .await
    .expect("register");
    assert!(registered == response);
    let wire = request.join().expect("mock request");
    let lower = wire.to_ascii_lowercase();
    assert!(wire.starts_with("POST /runner/v1/register HTTP/1.1\r\n"));
    assert!(lower.contains("authorization: bearer one-time\r\n"));
    assert!(lower.contains("x-flori-protocol: 1\r\n"));
    assert!(wire.ends_with(r#"{"tools":[],"ai_models":[]}"#));

    let (base_url, request) = serve_once(MockResponse {
        status: "204 No Content",
        body: String::new(),
    });
    let client = RunnerClient::new(&base_url, "runner-token").expect("client");
    assert!(client.poll().await.expect("poll").is_none());
    let wire = request.join().expect("mock request");
    assert!(wire.starts_with("POST /runner/v1/poll HTTP/1.1\r\n"));
    assert!(!wire.contains("content-length:"));
}

#[tokio::test]
async fn logs_and_upload_chunks_use_the_frozen_wire() {
    let line = r#"{"timestamp_ms":1,"level":"info","message":"ok"}"#.to_owned();
    let frame = LogFrame {
        sequence: 1,
        sha256: content_digest(line.as_bytes()),
        line,
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: r#"{"last_sequence":1}"#.to_owned(),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    let exec_id = AttemptId::generate();
    let cursor = client
        .append_logs(exec_id, std::slice::from_ref(&frame))
        .await
        .expect("append logs");
    assert_eq!(cursor.last_sequence, 1);
    let wire = request.join().expect("mock request");
    let lower = wire.to_ascii_lowercase();
    assert!(lower.contains("content-type: application/x-ndjson\r\n"));
    assert!(wire.ends_with(&format!(
        "{}\n",
        serde_json::to_string(&frame).expect("frame JSON")
    )));

    let upload_id = UploadId::generate();
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&UploadCursor {
            upload_id,
            received_bytes: 3,
        })
        .expect("cursor JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    let cursor = client
        .append_upload_chunk(upload_id, 0, b"abc".to_vec())
        .await
        .expect("append upload");
    assert_eq!(cursor.received_bytes, 3);
    let wire = request.join().expect("mock request");
    let lower = wire.to_ascii_lowercase();
    assert!(lower.contains("upload-offset: 0\r\n"));
    assert!(lower.contains(
        "x-flori-chunk-sha256: ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\r\n"
    ));
    assert!(wire.ends_with("abc"));
}

#[tokio::test]
async fn remote_error_codes_are_preserved() {
    let response = ErrorResponse {
        error: ErrorBody {
            code: ErrorCode::StaleAttempt,
            message: "stale".to_owned(),
            request_id: RequestId::generate(),
            field: None,
            retry_after_ms: None,
        },
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "409 Conflict",
        body: serde_json::to_string(&response).expect("error JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    let error = client
        .renew(AttemptId::generate())
        .await
        .expect_err("stale attempt");
    assert_eq!(error.code(), ErrorCode::StaleAttempt);
    assert_eq!(error.remote(), Some(&response.error));
    request.join().expect("mock request");
}

#[tokio::test]
async fn lease_usage_and_terminal_commands_use_core_dtos() {
    let exec_id = AttemptId::generate();
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&RenewLeaseResponse {
            lease_expires_at_ms: 42,
        })
        .expect("renew JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    assert_eq!(
        client
            .renew(exec_id)
            .await
            .expect("renew")
            .lease_expires_at_ms,
        42
    );
    assert!(
        request
            .join()
            .expect("renew request")
            .starts_with(&format!("POST /runner/v1/attempts/{exec_id}/renew "))
    );

    let usage_id = AiUsageId::generate();
    let update = UsageUpdate::Started {
        invocation_key: "one".to_owned(),
        tool: AiTool::CodexCli,
        model: "model".to_owned(),
        effort: "high".to_owned(),
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&UsageAck {
            usage_id,
            state: AiUsageState::Started,
        })
        .expect("usage JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    assert_eq!(
        client
            .update_usage(exec_id, &update)
            .await
            .expect("usage")
            .usage_id,
        usage_id
    );
    assert!(
        request
            .join()
            .expect("usage request")
            .ends_with(&serde_json::to_string(&update).expect("update JSON"))
    );

    let manifest = digest('d');
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&AttemptAck {
            exec_id,
            state: AttemptState::Succeeded,
        })
        .expect("complete JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    client
        .complete(exec_id, manifest.clone())
        .await
        .expect("complete");
    assert!(
        request
            .join()
            .expect("complete request")
            .contains(&format!(r#"{{"manifest_sha256":"{}"}}"#, manifest.as_str()))
    );

    let failure = FailAttemptRequest {
        error_code: ErrorCode::ExecutorFailed,
        manifest_sha256: None,
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&AttemptAck {
            exec_id,
            state: AttemptState::Failed,
        })
        .expect("fail JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    client.fail(exec_id, &failure).await.expect("fail");
    assert!(
        request
            .join()
            .expect("fail request")
            .ends_with(&serde_json::to_string(&failure).expect("failure JSON"))
    );
}

#[tokio::test]
async fn upload_start_verify_and_manifest_digest_are_deterministic() {
    let exec_id = AttemptId::generate();
    let upload_id = UploadId::generate();
    let declaration = StartUploadRequest {
        name: "log".to_owned(),
        media_type: "application/x-ndjson".to_owned(),
        size_bytes: 3,
        sha256: digest('a'),
    };
    let entry = artifact("log", 'a');
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&StartUploadResponse {
            upload_id,
            received_bytes: 0,
            artifact: entry.clone(),
        })
        .expect("start JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    assert_eq!(
        client
            .start_upload(exec_id, &declaration)
            .await
            .expect("start upload")
            .artifact,
        entry
    );
    assert!(
        request
            .join()
            .expect("start request")
            .ends_with(&serde_json::to_string(&declaration).expect("declaration JSON"))
    );

    let verification = VerifyUploadRequest {
        size_bytes: 3,
        sha256: digest('a'),
    };
    let (base_url, request) = serve_once(MockResponse {
        status: "200 OK",
        body: serde_json::to_string(&VerifyUploadResponse {
            upload_id,
            artifact: entry.clone(),
        })
        .expect("verify JSON"),
    });
    let client = RunnerClient::new(&base_url, "token").expect("client");
    client
        .verify_upload(upload_id, &verification)
        .await
        .expect("verify upload");
    assert!(
        request
            .join()
            .expect("verify request")
            .ends_with(&serde_json::to_string(&verification).expect("verification JSON"))
    );

    let job_id = JobId::generate();
    let task_id = TaskId::generate();
    let second = artifact("audit", 'b');
    let forward = manifest_sha256(
        job_id,
        task_id,
        exec_id,
        vec![entry.clone(), second.clone()],
    )
    .expect("manifest digest");
    let reverse = manifest_sha256(job_id, task_id, exec_id, vec![second, entry])
        .expect("sorted manifest digest");
    assert_eq!(forward, reverse);
}
