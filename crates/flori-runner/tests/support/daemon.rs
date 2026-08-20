#![allow(dead_code)]

use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{
    AiTool, ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactWhen, AttemptId, ErrorBody,
    ErrorCode, ErrorResponse, Executor, JobId, RequestId, ResolvedArtifact, ResolvedPrompt,
    ResolvedTaskInputs, SecretInputs, Sha256Digest, TaskClaim, TaskId,
};
use sha2::{Digest, Sha256};

pub(crate) fn claim(
    exec_id: AttemptId,
    executor: Executor,
    resolved_inputs: ResolvedTaskInputs,
    output_declarations: Vec<ArtifactDeclaration>,
    timeout_ms: u64,
) -> TaskClaim {
    TaskClaim {
        job_id: JobId::generate(),
        task_id: TaskId::generate(),
        task_key: "ai".into(),
        exec_id,
        attempt_no: 1,
        executor,
        timeout_ms,
        lease_expires_at_ms: now_ms() + 60_000,
        prompt_snapshot_sha256: digest(b"snapshot"),
        resolved_inputs,
        output_declarations,
        model: Some("model-1".into()),
        effort: Some("high".into()),
        runner_config_revision: 1,
        secret_inputs: SecretInputs::default(),
    }
}

pub(crate) fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_millis()
        .try_into()
        .expect("timestamp")
}

pub(crate) fn artifact(base_url: &str, bytes: &[u8]) -> ResolvedArtifact {
    let artifact_id = ArtifactId::generate();
    ResolvedArtifact {
        artifact_id,
        name: "structure".into(),
        kind: ArtifactKind::DocumentStructure,
        media_type: "application/json".into(),
        size_bytes: bytes.len() as u64,
        sha256: digest(bytes),
        download_url: format!("{base_url}/api/v1/artifacts/{artifact_id}/content"),
    }
}

pub(crate) fn prompt(content: &str) -> ResolvedPrompt {
    ResolvedPrompt {
        key: "prompt".into(),
        content: content.into(),
        sha256: digest(content.as_bytes()),
    }
}

pub(crate) fn declaration(
    name: &str,
    kind: ArtifactKind,
    when: ArtifactWhen,
) -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: name.into(),
        kind,
        path: format!("output/{name}"),
        required: true,
        when,
        max_files: None,
        max_bytes: 1024 * 1024,
    }
}

pub(crate) fn config(
    root: &Path,
    executable: PathBuf,
    renew_interval: Duration,
) -> crate::DaemonConfig {
    crate::DaemonConfig {
        tool: AiTool::QoderCli,
        executable,
        home: make_dir(root, "home"),
        tool_config_home: make_dir(root, "config"),
        work_root: make_dir(root, "work"),
        model: "model-1".into(),
        effort: "high".into(),
        renew_interval,
        max_output_bytes: 1024 * 1024,
    }
}

pub(crate) fn read_request(stream: &mut TcpStream) -> (String, Vec<u8>) {
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
    let head = String::from_utf8(request[..header_end].to_vec()).expect("headers");
    let length = head
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
    (head, request[header_end..].to_vec())
}

pub(crate) fn content_response(stream: &mut TcpStream, body: &[u8], sha256: &Sha256Digest) {
    write!(
        stream,
        "HTTP/1.1 206 Partial Content\r\nContent-Type: application/json\r\nContent-Length: {}\r\nAccept-Ranges: bytes\r\nContent-Range: bytes 0-{}/{}\r\nETag: \"{}\"\r\nConnection: close\r\n\r\n",
        body.len(),
        body.len() - 1,
        body.len(),
        sha256.as_str()
    )
    .expect("content headers");
    stream.write_all(body).expect("content body");
}

pub(crate) fn json_response(stream: &mut TcpStream, body: &impl serde::Serialize) {
    let body = serde_json::to_vec(body).expect("response JSON");
    response(stream, "200 OK", &body);
}

pub(crate) fn error_response(stream: &mut TcpStream, code: ErrorCode) {
    let body = serde_json::to_vec(&ErrorResponse {
        error: ErrorBody {
            code,
            message: "rejected".into(),
            request_id: RequestId::generate(),
            field: None,
            retry_after_ms: None,
        },
    })
    .expect("error JSON");
    response(stream, "409 Conflict", &body);
}

fn response(stream: &mut TcpStream, status: &str, body: &[u8]) {
    write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .expect("headers");
    stream.write_all(body).expect("body");
}

pub(crate) fn script(root: &Path, name: &str, body: &str) -> PathBuf {
    let path = root.join(name);
    fs::write(&path, format!("#!/bin/sh\n{body}")).expect("script");
    let mut permissions = fs::metadata(&path).expect("metadata").permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&path, permissions).expect("chmod");
    path
}

pub(crate) fn digest(bytes: &[u8]) -> Sha256Digest {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("digest")
}

pub(crate) fn temp_root(suffix: &str) -> PathBuf {
    let root =
        std::env::temp_dir().join(format!("flori-daemon-{suffix}-{}", RequestId::generate()));
    fs::create_dir(&root).expect("root");
    root
}

fn make_dir(root: &Path, name: &str) -> PathBuf {
    let path = root.join(name);
    fs::create_dir(&path).expect("directory");
    path
}
