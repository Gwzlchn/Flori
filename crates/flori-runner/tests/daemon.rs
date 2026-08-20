pub use flori_runner::*;

#[path = "../src/daemon/mod.rs"]
mod daemon;

pub use daemon::*;

use std::{
    fs,
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    thread,
    time::Duration,
};

use flori_core::{
    AiAudit, AiTool, AiUsageId, AiUsageState, ArtifactDeclaration, ArtifactId, ArtifactKind,
    ArtifactManifestEntry, ArtifactWhen, AttemptAck, AttemptState, ErrorCode, Executor, JobId,
    ResolvedArtifact, ResolvedPrompt, ResolvedTaskInputs, SecretInputs, Sha256Digest,
    StartUploadRequest, StartUploadResponse, TaskClaim, TaskId, UploadCursor, UploadId, UsageAck,
    VerifyUploadResponse,
};
use sha2::{Digest, Sha256};
use tokio::sync::watch;

#[tokio::test]
async fn replayed_usage_never_spawns_and_fails_with_only_audit() {
    let root = temp_root();
    let marker = root.join("spawned");
    let executable = root.join("fake-qoder");
    fs::write(
        &executable,
        format!("#!/bin/sh\ntouch {}\n", marker.display()),
    )
    .expect("fake executable");
    let mut permissions = fs::metadata(&executable).expect("metadata").permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&executable, permissions).expect("chmod");

    let document = br#"{"schema":"document"}"#.to_vec();
    let document_sha = digest(&document);
    let artifact_id = ArtifactId::generate();
    let exec_id = flori_core::AttemptId::generate();
    let upload_id = UploadId::generate();
    let (base_url, server) =
        replay_server(document.clone(), document_sha.clone(), exec_id, upload_id);
    let prompt = "write a note";
    let claim = TaskClaim {
        job_id: JobId::generate(),
        task_id: TaskId::generate(),
        task_key: "note".into(),
        exec_id,
        attempt_no: 1,
        executor: Executor::AiDocumentNote,
        timeout_ms: 2_000,
        lease_expires_at_ms: i64::MAX,
        prompt_snapshot_sha256: digest(b"snapshot"),
        resolved_inputs: ResolvedTaskInputs::AiDocumentNote {
            document: ResolvedArtifact {
                artifact_id,
                name: "structure".into(),
                kind: ArtifactKind::DocumentStructure,
                media_type: "application/json".into(),
                size_bytes: document.len() as u64,
                sha256: document_sha,
                download_url: format!("{base_url}/api/v1/artifacts/{artifact_id}/content"),
            },
            prompt: ResolvedPrompt {
                key: "document_note".into(),
                content: prompt.into(),
                sha256: digest(prompt.as_bytes()),
            },
            profile: None,
        },
        output_declarations: vec![ArtifactDeclaration {
            name: "audit".into(),
            kind: ArtifactKind::AiAudit,
            path: "logs/ai-audit.json".into(),
            required: true,
            when: ArtifactWhen::Always,
            max_files: None,
            max_bytes: 1024 * 1024,
        }],
        model: Some("model-1".into()),
        effort: Some("high".into()),
        runner_config_revision: 1,
        secret_inputs: SecretInputs::default(),
    };
    let config = DaemonConfig {
        tool: AiTool::QoderCli,
        executable,
        home: make_dir(&root, "home"),
        tool_config_home: make_dir(&root, "config"),
        work_root: make_dir(&root, "work"),
        renew_interval: Duration::from_secs(1),
        max_output_bytes: 1024 * 1024,
    };
    let client = RunnerClient::new(&base_url, "runner-token").expect("client");
    let (_keep, mut cancel) = watch::channel(false);
    assert_eq!(
        run_claim(&client, &config, claim, &mut cancel).await,
        Err(ErrorCode::UsageConflict)
    );
    let audit = server.join().expect("server");
    assert_eq!(audit.usage_invocation_keys, ["primary"]);
    assert!(audit.redacted_arguments.is_empty());
    assert!(!marker.exists(), "idempotent replay must not spawn CLI");
    fs::remove_dir_all(root).expect("cleanup");
}

fn replay_server(
    document: Vec<u8>,
    document_sha: Sha256Digest,
    exec_id: flori_core::AttemptId,
    upload_id: UploadId,
) -> (String, thread::JoinHandle<AiAudit>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let address = listener.local_addr().expect("address");
    let handle = thread::spawn(move || {
        let mut uploaded = Vec::new();
        let mut entry = None;
        for step in 0..6 {
            let (mut stream, _) = listener.accept().expect("accept");
            let request = read_request(&mut stream);
            let header_end = request
                .windows(4)
                .position(|bytes| bytes == b"\r\n\r\n")
                .expect("headers")
                + 4;
            let head = String::from_utf8_lossy(&request[..header_end]);
            let body = &request[header_end..];
            let (status, response, headers) = match step {
                0 => {
                    assert!(head.starts_with("GET /api/v1/artifacts/"));
                    let headers = format!(
                        "Accept-Ranges: bytes\r\nContent-Range: bytes 0-{}/{}\r\nETag: \"{}\"\r\n",
                        document.len() - 1,
                        document.len(),
                        document_sha.as_str()
                    );
                    ("206 Partial Content", document.clone(), headers)
                }
                1 => {
                    assert!(
                        head.starts_with(&format!("POST /runner/v1/attempts/{exec_id}/usage "))
                    );
                    assert!(String::from_utf8_lossy(body).contains(r#""state":"started""#));
                    (
                        "200 OK",
                        serde_json::to_vec(&UsageAck {
                            usage_id: AiUsageId::generate(),
                            state: AiUsageState::Started,
                            applied: false,
                        })
                        .expect("usage ack"),
                        String::new(),
                    )
                }
                2 => {
                    let start: StartUploadRequest =
                        serde_json::from_slice(body).expect("start upload");
                    let artifact = ArtifactManifestEntry {
                        name: start.name,
                        kind: ArtifactKind::AiAudit,
                        media_type: start.media_type,
                        size_bytes: start.size_bytes,
                        sha256: start.sha256,
                        relative_path: "sources/job/audit.json".into(),
                    };
                    entry = Some(artifact.clone());
                    (
                        "200 OK",
                        serde_json::to_vec(&StartUploadResponse {
                            upload_id,
                            received_bytes: 0,
                            artifact,
                        })
                        .expect("upload response"),
                        String::new(),
                    )
                }
                3 => {
                    uploaded = body.to_vec();
                    (
                        "200 OK",
                        serde_json::to_vec(&UploadCursor {
                            upload_id,
                            received_bytes: uploaded.len() as u64,
                        })
                        .expect("upload cursor"),
                        String::new(),
                    )
                }
                4 => (
                    "200 OK",
                    serde_json::to_vec(&VerifyUploadResponse {
                        upload_id,
                        artifact: entry.clone().expect("entry"),
                    })
                    .expect("verify response"),
                    String::new(),
                ),
                5 => {
                    assert!(
                        String::from_utf8_lossy(body).contains(r#""error_code":"usage_conflict""#)
                    );
                    (
                        "200 OK",
                        serde_json::to_vec(&AttemptAck {
                            exec_id,
                            state: AttemptState::Failed,
                        })
                        .expect("fail response"),
                        String::new(),
                    )
                }
                _ => unreachable!(),
            };
            write!(
                stream,
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n{headers}Connection: close\r\n\r\n",
                response.len()
            )
            .expect("headers");
            stream.write_all(&response).expect("body");
        }
        serde_json::from_slice(&uploaded).expect("strict audit")
    });
    (format!("http://{address}"), handle)
}

fn read_request(stream: &mut std::net::TcpStream) -> Vec<u8> {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 8192];
    let header_end = loop {
        let read = stream.read(&mut buffer).expect("read request");
        assert!(read > 0);
        request.extend_from_slice(&buffer[..read]);
        if let Some(position) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
            break position + 4;
        }
    };
    let headers = String::from_utf8_lossy(&request[..header_end]);
    let length = headers
        .lines()
        .find_map(|line| {
            line.to_ascii_lowercase()
                .strip_prefix("content-length: ")
                .and_then(|value| value.parse::<usize>().ok())
        })
        .unwrap_or(0);
    while request.len() < header_end + length {
        let read = stream.read(&mut buffer).expect("read body");
        assert!(read > 0);
        request.extend_from_slice(&buffer[..read]);
    }
    request
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("digest")
}

fn temp_root() -> PathBuf {
    let root = std::env::temp_dir().join(format!(
        "flori-daemon-http-{}",
        flori_core::RequestId::generate()
    ));
    fs::create_dir(&root).expect("root");
    root
}

fn make_dir(root: &std::path::Path, name: &str) -> PathBuf {
    let path = root.join(name);
    fs::create_dir(&path).expect("directory");
    path
}
