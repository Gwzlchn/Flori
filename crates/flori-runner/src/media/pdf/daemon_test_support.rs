use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    thread,
};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptAck, AttemptState, CompleteAttemptRequest,
    FailAttemptRequest, LogCursor, Sha256Digest, StartUploadRequest, StartUploadResponse,
    TaskClaim, UploadCursor, UploadId, VerifyUploadRequest, VerifyUploadResponse,
};
use sha2::{Digest, Sha256};

pub(super) struct TestRoot(pub(super) PathBuf);

impl TestRoot {
    pub(super) fn new(name: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "flori-pdf-daemon-{name}-{}",
            flori_core::AttemptId::generate()
        ));
        fs::create_dir(&path).expect("test root");
        Self(path)
    }

    pub(super) fn script(&self, name: &str, body: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("fake tool");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).expect("executable");
        path
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove test root");
    }
}

pub(super) struct SuccessCase {
    pub(super) claim: TaskClaim,
    pub(super) input: Vec<u8>,
    pub(super) input_path: String,
    pub(super) input_media_type: &'static str,
    pub(super) output_kind: ArtifactKind,
    pub(super) output_media_type: &'static str,
}

pub(super) fn success_server(
    listener: TcpListener,
    case: SuccessCase,
) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut claim_sent = false;
        let mut uploaded = Vec::new();
        let mut upload = None::<(UploadId, ArtifactManifestEntry)>;
        loop {
            let (mut stream, _) = listener.accept().expect("accept");
            let (head, body) = request(&mut stream);
            assert_eq!(header(&head, "authorization"), "Bearer runner-token");
            assert_eq!(
                header(&head, "x-flori-protocol"),
                flori_core::PROTOCOL_VERSION
            );
            let request_line = head.lines().next().expect("request line");
            if request_line == "POST /runner/v1/poll HTTP/1.1" && !claim_sent {
                claim_sent = true;
                json(&mut stream, &case.claim);
            } else if request_line.contains("/logs HTTP/1.1") {
                let line = body.split(|byte| *byte == b'\n').next().expect("log line");
                let frame: flori_core::LogFrame = serde_json::from_slice(line).expect("log frame");
                assert_eq!(frame.sequence, 1);
                let task_line: flori_core::TaskLogLine =
                    serde_json::from_str(&frame.line).expect("task log");
                assert_eq!(task_line.message, "PDF task started");
                json(&mut stream, &LogCursor { last_sequence: 1 });
            } else if request_line == format!("GET {} HTTP/1.1", case.input_path) {
                content(
                    &mut stream,
                    &case.input,
                    case.input_media_type,
                    &digest(&case.input),
                );
            } else if request_line.contains("POST /runner/v1/attempts/")
                && request_line.contains("/uploads HTTP/1.1")
            {
                let start: StartUploadRequest = serde_json::from_slice(&body).expect("start");
                assert_eq!(start.media_type, case.output_media_type);
                let upload_id = UploadId::generate();
                let entry = ArtifactManifestEntry {
                    name: start.name,
                    kind: case.output_kind,
                    media_type: start.media_type,
                    size_bytes: start.size_bytes,
                    sha256: start.sha256,
                    relative_path: "sources/test/output".into(),
                };
                upload = Some((upload_id, entry.clone()));
                json(
                    &mut stream,
                    &StartUploadResponse {
                        upload_id,
                        received_bytes: 0,
                        artifact: entry,
                    },
                );
            } else if request_line.starts_with("PUT /runner/v1/uploads/") {
                let offset = header(&head, "upload-offset")
                    .parse::<usize>()
                    .expect("offset");
                assert_eq!(offset, uploaded.len());
                assert_eq!(
                    header(&head, "x-flori-chunk-sha256"),
                    digest(&body).as_str()
                );
                uploaded.extend_from_slice(&body);
                json(
                    &mut stream,
                    &UploadCursor {
                        upload_id: upload.as_ref().expect("upload").0,
                        received_bytes: uploaded.len() as u64,
                    },
                );
            } else if request_line.contains("/verify HTTP/1.1") {
                let verify: VerifyUploadRequest = serde_json::from_slice(&body).expect("verify");
                assert_eq!(verify.size_bytes, uploaded.len() as u64);
                assert_eq!(verify.sha256, digest(&uploaded));
                let (upload_id, entry) = upload.as_ref().expect("upload");
                json(
                    &mut stream,
                    &VerifyUploadResponse {
                        upload_id: *upload_id,
                        artifact: entry.clone(),
                    },
                );
            } else if request_line.contains("/complete HTTP/1.1") {
                let complete: CompleteAttemptRequest =
                    serde_json::from_slice(&body).expect("complete");
                let expected = crate::manifest_sha256(
                    case.claim.job_id,
                    case.claim.task_id,
                    case.claim.exec_id,
                    vec![upload.as_ref().expect("upload").1.clone()],
                )
                .expect("manifest");
                assert_eq!(complete.manifest_sha256, expected);
                json(
                    &mut stream,
                    &AttemptAck {
                        exec_id: case.claim.exec_id,
                        state: AttemptState::Succeeded,
                    },
                );
                return uploaded;
            } else {
                panic!("unexpected request: {request_line}");
            }
        }
    })
}

pub(super) fn failure_server(
    listener: TcpListener,
    claim: TaskClaim,
    expected: flori_core::ErrorCode,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let (mut poll, _) = listener.accept().expect("poll accept");
        request(&mut poll);
        json(&mut poll, &claim);
        let (mut fail, _) = listener.accept().expect("fail accept");
        let (head, body) = request(&mut fail);
        assert!(head.contains("/fail HTTP/1.1"));
        let request: FailAttemptRequest = serde_json::from_slice(&body).expect("failure");
        assert_eq!(request.error_code, expected);
        assert!(request.manifest_sha256.is_none());
        json(
            &mut fail,
            &AttemptAck {
                exec_id: claim.exec_id,
                state: AttemptState::Failed,
            },
        );
    })
}

pub(super) fn poll_server(listener: TcpListener, claim: TaskClaim) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let (mut poll, _) = listener.accept().expect("poll accept");
        request(&mut poll);
        json(&mut poll, &claim);
    })
}

fn request(stream: &mut TcpStream) -> (String, Vec<u8>) {
    let mut request = Vec::new();
    let mut buffer = [0_u8; 64 * 1024];
    let header_end = loop {
        let count = stream.read(&mut buffer).expect("request");
        assert!(count > 0);
        request.extend_from_slice(&buffer[..count]);
        if let Some(position) = request.windows(4).position(|value| value == b"\r\n\r\n") {
            break position + 4;
        }
    };
    let head = String::from_utf8(request[..header_end].to_vec()).expect("headers");
    let length = header(&head, "content-length").parse().unwrap_or(0);
    while request.len() < header_end + length {
        let count = stream.read(&mut buffer).expect("body");
        assert!(count > 0);
        request.extend_from_slice(&buffer[..count]);
    }
    (head, request[header_end..].to_vec())
}

fn header<'a>(head: &'a str, name: &str) -> &'a str {
    head.lines()
        .find_map(|line| {
            let (key, value) = line.split_once(": ")?;
            key.eq_ignore_ascii_case(name).then_some(value)
        })
        .unwrap_or("")
}

fn content(stream: &mut TcpStream, body: &[u8], media_type: &str, sha256: &Sha256Digest) {
    write!(
        stream,
        "HTTP/1.1 206 Partial Content\r\nContent-Type: {media_type}\r\nContent-Length: {}\r\nAccept-Ranges: bytes\r\nContent-Range: bytes 0-{}/{}\r\nETag: \"{}\"\r\nConnection: close\r\n\r\n",
        body.len(),
        body.len() - 1,
        body.len(),
        sha256.as_str()
    )
    .expect("content headers");
    stream.write_all(body).expect("content body");
}

fn json(stream: &mut TcpStream, value: &impl serde::Serialize) {
    let body = serde_json::to_vec(value).expect("response JSON");
    write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    )
    .expect("response headers");
    stream.write_all(&body).expect("response body");
}

pub(super) fn digest(bytes: &[u8]) -> Sha256Digest {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("digest")
}
