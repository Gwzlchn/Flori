use flori_core::{
    CreateUploadSource, CreatedSource, DomainId, ErrorCode, ErrorResponse, Sha256Digest, SourceKind,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};
use std::{fs, net::SocketAddr, path::PathBuf, sync::Arc};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};
const BOUNDARY: &str = "flori-source-upload-boundary";
struct Harness {
    root: PathBuf,
    artifact_root: PathBuf,
    pool: SqlitePool,
    domain_id: DomainId,
    address: SocketAddr,
    server: JoinHandle<()>,
}
impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "flori-source-upload-http-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&root).expect("test root");
        let database = root.join("flori.sqlite");
        let artifact_root = root.join("artifacts");
        let store = Arc::new(Store::open(&database).await.expect("empty SQLite"));
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("inspection pool");
        let domain_id = DomainId::generate();
        sqlx::query(
            "INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) \
             VALUES(?,?,'PDF','',0,0)",
        )
        .bind(domain_id.to_string())
        .bind(format!("pdf-{domain_id}"))
        .execute(&pool)
        .await
        .expect("domain");
        let artifacts = Arc::new(
            NasArtifactStore::new(&artifact_root, 1024 * 1024).expect("NAS artifact root"),
        );
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(store, artifacts, "http://localhost/content".into(), 60_000)
                    .expect("app"),
            )
            .await
            .expect("serve");
        });
        Self {
            root,
            artifact_root,
            pool,
            domain_id,
            address,
            server,
        }
    }
    fn metadata(&self, request_key: &str, bytes: &[u8]) -> Vec<u8> {
        serde_json::to_vec(&CreateUploadSource {
            request_key: request_key.into(),
            kind: SourceKind::PdfUpload,
            title: Some("Paper".into()),
            domain_id: self.domain_id,
            collection_ids: Vec::new(),
            file_sha256: digest(bytes),
        })
        .expect("metadata")
    }
    async fn post(&self, parts: &[Part<'_>], protocol: bool) -> Vec<u8> {
        let body = multipart(parts);
        let protocol = if protocol {
            "X-Flori-Protocol: 1\r\n"
        } else {
            ""
        };
        let head = format!(
            "POST /api/v1/sources/uploads HTTP/1.1\r\nHost: localhost\r\n\
             Connection: close\r\n{protocol}Content-Type: multipart/form-data; boundary={BOUNDARY}\r\n\
             Content-Length: {}\r\n\r\n",
            body.len()
        );
        let mut stream = TcpStream::connect(self.address).await.expect("connect");
        stream.write_all(head.as_bytes()).await.expect("headers");
        stream.write_all(&body).await.expect("body");
        let mut response = Vec::new();
        stream.read_to_end(&mut response).await.expect("response");
        response
    }
    async fn assert_empty(&self) {
        assert_eq!(counts(&self.pool).await, (0, 0, 0));
        assert!(files_under(&self.artifact_root).is_empty());
    }
}
impl Drop for Harness {
    fn drop(&mut self) {
        self.server.abort();
        let _ = fs::remove_dir_all(&self.root);
    }
}
#[tokio::test]
async fn raw_pdf_upload_commits_source_input_and_replays_idempotently() {
    let harness = Harness::new().await;
    let pdf = b"%PDF-1.7\ndigital paper";
    let metadata = harness.metadata("upload-http-one", pdf);
    let response = harness
        .post(&upload_parts(&metadata, "application/pdf", pdf), true)
        .await;
    assert_eq!(status(&response), 200);
    let created: CreatedSource = serde_json::from_slice(body(&response)).expect("created source");
    let replay = harness
        .post(&upload_parts(&metadata, "application/pdf", pdf), true)
        .await;
    assert_eq!(status(&replay), 200);
    let replay: CreatedSource = serde_json::from_slice(body(&replay)).expect("replayed source");
    assert_eq!(replay, created);
    let row =
        sqlx::query("SELECT i.relative_path,i.sha256 FROM source_inputs i WHERE i.source_id=?")
            .bind(created.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("source input");
    let relative_path: String = row.try_get("relative_path").expect("path");
    let stored_sha: String = row.try_get("sha256").expect("SHA-256");
    assert_eq!(stored_sha, digest(pdf).as_str());
    let stored = fs::read(harness.artifact_root.join(relative_path)).unwrap();
    assert_eq!(stored, pdf);
    assert_eq!(counts(&harness.pool).await, (1, 1, 0));
    assert_eq!(files_under(&harness.artifact_root).len(), 1);
    let drifted_pdf = b"%PDF-1.7\ndifferent";
    let drifted = harness.metadata("upload-http-one", drifted_pdf);
    let response = harness
        .post(
            &upload_parts(&drifted, "application/pdf", drifted_pdf),
            true,
        )
        .await;
    assert_error(&response, 409, ErrorCode::IdempotencyConflict);
    assert_eq!(counts(&harness.pool).await, (1, 1, 0));
    assert_eq!(files_under(&harness.artifact_root).len(), 1);
}
#[tokio::test]
async fn malformed_multipart_fails_without_source_or_final_file() {
    let harness = Harness::new().await;
    let pdf = b"%PDF-1.7\ndigital paper";
    let metadata = harness.metadata("missing-protocol", pdf);
    let response = harness
        .post(&upload_parts(&metadata, "application/pdf", pdf), false)
        .await;
    assert_error(&response, 400, ErrorCode::ProtocolMismatch);
    harness.assert_empty().await;
    let unknown = String::from_utf8(harness.metadata("unknown-json", pdf))
        .unwrap()
        .replacen('}', ",\"legacy\":true}", 1);
    let bad_magic = harness.metadata("bad-magic", b"not-a-pdf");
    let bad_type = harness.metadata("bad-type", pdf);
    let cases = [
        (
            vec![
                Part::metadata(unknown.as_bytes()),
                Part::file("application/pdf", pdf),
            ],
            ErrorCode::InvalidRequest,
        ),
        (
            vec![
                Part::metadata(&metadata),
                Part::metadata(&metadata),
                Part::file("application/pdf", pdf),
            ],
            ErrorCode::InvalidRequest,
        ),
        (
            vec![
                Part::metadata(&bad_magic),
                Part::file("application/pdf", b"not-a-pdf"),
            ],
            ErrorCode::DigestMismatch,
        ),
        (
            vec![Part::metadata(&bad_type), Part::file("text/plain", pdf)],
            ErrorCode::InvalidRequest,
        ),
    ];
    for (parts, code) in cases {
        let response = harness.post(&parts, true).await;
        assert_error(&response, 400, code);
        harness.assert_empty().await;
    }
}
struct Part<'a> {
    name: &'static str,
    content_type: &'static str,
    bytes: &'a [u8],
}
impl<'a> Part<'a> {
    fn metadata(bytes: &'a [u8]) -> Self {
        Self {
            name: "metadata",
            content_type: "application/json",
            bytes,
        }
    }
    fn file(content_type: &'static str, bytes: &'a [u8]) -> Self {
        Self {
            name: "file",
            content_type,
            bytes,
        }
    }
}
fn upload_parts<'a>(metadata: &'a [u8], media_type: &'static str, file: &'a [u8]) -> [Part<'a>; 2] {
    [Part::metadata(metadata), Part::file(media_type, file)]
}
fn multipart(parts: &[Part<'_>]) -> Vec<u8> {
    let mut body = Vec::new();
    for part in parts {
        body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
        let filename = if part.name == "file" {
            "; filename=\"paper.pdf\""
        } else {
            ""
        };
        body.extend_from_slice(
            format!(
                "Content-Disposition: form-data; name=\"{}\"{filename}\r\n\
                 Content-Type: {}\r\n\r\n",
                part.name, part.content_type
            )
            .as_bytes(),
        );
        body.extend_from_slice(part.bytes);
        body.extend_from_slice(b"\r\n");
    }
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    body
}
async fn counts(pool: &SqlitePool) -> (i64, i64, i64) {
    sqlx::query_as(
        "SELECT (SELECT count(*) FROM sources), \
         (SELECT count(*) FROM source_inputs), (SELECT count(*) FROM uploads)",
    )
    .fetch_one(pool)
    .await
    .expect("count")
}
fn digest(bytes: &[u8]) -> Sha256Digest {
    let hex = Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Sha256Digest::parse(hex).expect("digest")
}
fn files_under(root: &std::path::Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in fs::read_dir(root).expect("artifact root") {
        let path = entry.expect("entry").path();
        if path.is_dir() {
            files.extend(files_under(&path));
        } else {
            files.push(path);
        }
    }
    files
}
fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response.split(|byte| *byte == b'\n').next().unwrap())
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap()
}
fn body(response: &[u8]) -> &[u8] {
    let start = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("response body");
    &response[start + 4..]
}
fn assert_error(response: &[u8], expected_status: u16, expected_code: ErrorCode) {
    assert_eq!(status(response), expected_status);
    let error: ErrorResponse = serde_json::from_slice(body(response)).expect("error response");
    assert_eq!(error.error.code, expected_code);
}
