use std::{fs, net::SocketAddr, path::PathBuf, sync::Arc};

use flori_core::{
    DocumentRepresentationView, ErrorCode, ErrorResponse, EvidenceId, HtmlPdfCrosswalkStatus,
    JobId, ScholarlyFile, ScholarlyHtmlSnapshot, ScholarlyHtmlSnapshotSchema, ScholarlyProvider,
    Sha256Digest, SourceId, TaskId,
};
use flori_store::{
    Store,
    artifact::{NasArtifactStore, retained_artifact_path},
};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, raw_sql};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};

#[tokio::test]
async fn current_scholarly_document_is_verified_sanitized_and_crosswalked() {
    let harness = Harness::new().await;
    let target = format!(
        "/api/v1/sources/{}/document?evidence_id={}",
        harness.source_id, harness.evidence_id
    );
    let response = harness.get(&target).await;
    assert_eq!(status(&response), 200);
    let view: DocumentRepresentationView = serde_json::from_slice(body(&response)).expect("view");
    let DocumentRepresentationView::ScholarlyHtml {
        provider,
        resources,
        crosswalk,
        ..
    } = view
    else {
        panic!("HTML representation");
    };
    assert_eq!(provider, ScholarlyProvider::Arxiv);
    assert_eq!(resources.len(), 1);
    let crosswalk = crosswalk.expect("crosswalk");
    assert_eq!(crosswalk.status, HtmlPdfCrosswalkStatus::Verified);
    assert_eq!(crosswalk.html_anchor.as_deref(), Some("sec-transformer"));

    let response = harness
        .get(&format!(
            "/api/v1/sources/{}/document/content",
            harness.source_id
        ))
        .await;
    assert_eq!(status(&response), 200);
    let wire = String::from_utf8(response).expect("HTTP text");
    for required in [
        "Content-Security-Policy:",
        "cross-origin-resource-policy: same-origin",
        "data-flori-resource=\"scholarly_resources/figure.png\"",
    ] {
        assert!(
            wire.to_ascii_lowercase()
                .contains(&required.to_ascii_lowercase()),
            "{required}"
        );
    }
    for forbidden in ["<script", "evil.example", " src="] {
        assert!(!String::from_utf8_lossy(body(wire.as_bytes())).contains(forbidden));
    }

    fs::write(&harness.html_path, b"tampered").expect("tamper HTML");
    assert_error(&harness.get(&target).await, 400, ErrorCode::DigestMismatch);
    assert_error(
        &harness
            .get(&format!(
                "/api/v1/sources/{}/document?unknown=1",
                harness.source_id
            ))
            .await,
        400,
        ErrorCode::InvalidRequest,
    );
}

struct Harness {
    root: PathBuf,
    source_id: SourceId,
    evidence_id: EvidenceId,
    html_path: PathBuf,
    address: SocketAddr,
    server: JoinHandle<()>,
}

impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-scholarly-{}", JobId::generate()));
        fs::create_dir(&root).expect("root");
        let database = root.join("flori.sqlite");
        let store = Arc::new(Store::open(&database).await.expect("store"));
        let pool = SqlitePool::connect(&format!("sqlite://{}", database.display()))
            .await
            .expect("pool");
        let artifact_root = root.join("artifacts");
        let source_id = "018f0000-0000-7000-8000-000000000004"
            .parse()
            .expect("source ID");
        let job_id = "018f0000-0000-7000-8000-000000000005"
            .parse()
            .expect("job ID");
        let task_id: TaskId = "018f0000-0000-7000-8000-000000000007"
            .parse()
            .expect("task ID");
        let evidence_id: EvidenceId = "018f0000-0000-7000-8000-000000000008"
            .parse()
            .expect("evidence ID");
        let pdf_id = "018f0000-0000-7000-8000-000000000009"
            .parse()
            .expect("PDF ID");
        let html_id = "018f0000-0000-7000-8000-00000000000a"
            .parse()
            .expect("HTML ID");
        let snapshot_id = "018f0000-0000-7000-8000-00000000000b"
            .parse()
            .expect("snapshot ID");
        let image_id = "018f0000-0000-7000-8000-00000000000c"
            .parse()
            .expect("image ID");
        let html = br#"<html><head></head><body class="ltx_document"><script>bad()</script><p id="sec-transformer">The Transformer uses attention.</p><a href="https://evil.example">leave</a><img src="https://evil.example/x" data-flori-resource="scholarly_resources/figure.png"></body></html>"#;
        let pdf = b"%PDF-1.7\nfixture";
        let image = b"\x89PNG\r\n\x1a\nfixture";
        let snapshot = ScholarlyHtmlSnapshot {
            schema: ScholarlyHtmlSnapshotSchema::V1,
            job_id,
            provider: ScholarlyProvider::Arxiv,
            document_url: "https://arxiv.org/html/1706.03762".into(),
            html: ScholarlyFile {
                artifact_name: "scholarly_html".into(),
                media_type: "text/html".into(),
                size_bytes: html.len() as u64,
                sha256: digest(html),
            },
            resources: vec![ScholarlyFile {
                artifact_name: "scholarly_resources/figure.png".into(),
                media_type: "image/png".into(),
                size_bytes: image.len() as u64,
                sha256: digest(image),
            }],
        };
        let snapshot = serde_json::to_vec(&snapshot).expect("snapshot");
        raw_sql(include_str!("fixtures/scholarly_document.sql"))
            .execute(&pool)
            .await
            .expect("base fixture");
        let mut html_path = None;
        for (id, name, kind, media, file, bytes) in [
            (
                pdf_id,
                "original",
                "source_original",
                "application/pdf",
                "source.pdf",
                pdf.as_slice(),
            ),
            (
                html_id,
                "scholarly_html",
                "scholarly_html",
                "text/html",
                "document.html",
                html.as_slice(),
            ),
            (
                snapshot_id,
                "scholarly_snapshot",
                "scholarly_html_snapshot",
                "application/json",
                "snapshot.json",
                snapshot.as_slice(),
            ),
            (
                image_id,
                "scholarly_resources/figure.png",
                "scholarly_resource",
                "image/png",
                "figure.png",
                image.as_slice(),
            ),
        ] {
            let relative = retained_artifact_path(source_id, id, file).expect("artifact path");
            let path = artifact_root.join(&relative);
            fs::create_dir_all(path.parent().expect("parent")).expect("parents");
            fs::write(&path, bytes).expect("artifact bytes");
            sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,'materialized',?,?,?,?,?,?,?,'source',0)")
                .bind(id.to_string()).bind(source_id.to_string()).bind(job_id.to_string()).bind(task_id.to_string())
                .bind(name).bind(kind).bind(media).bind(file).bind(bytes.len() as i64).bind(digest(bytes).as_str()).bind(&relative)
                .execute(&pool).await.expect("artifact");
            if id == html_id {
                html_path = Some(path);
            }
        }
        sqlx::query("INSERT INTO evidence(id,source_id,job_id,artifact_id,locator_kind,page,x1,y1,x2,y2,quote) VALUES(?,?,?,?,'pdf',1,0,0,10,10,'The Transformer uses attention.')")
            .bind(evidence_id.to_string()).bind(source_id.to_string()).bind(job_id.to_string()).bind(pdf_id.to_string())
            .execute(&pool).await.expect("evidence");
        pool.close().await;
        let artifacts =
            Arc::new(NasArtifactStore::new(&artifact_root, 64 * 1024 * 1024).expect("NAS"));
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
            source_id,
            evidence_id,
            html_path: html_path.expect("HTML path"),
            address,
            server,
        }
    }

    async fn get(&self, target: &str) -> Vec<u8> {
        let mut stream = TcpStream::connect(self.address).await.expect("connect");
        let request = format!(
            "GET {target} HTTP/1.1\r\nHost: localhost\r\nX-Flori-Protocol: 1\r\nConnection: close\r\n\r\n"
        );
        stream.write_all(request.as_bytes()).await.expect("request");
        let mut response = Vec::new();
        stream.read_to_end(&mut response).await.expect("response");
        response
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        self.server.abort();
        let _ = fs::remove_dir_all(&self.root);
    }
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
fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response.split(|b| *b == b'\n').next().unwrap())
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap()
}
fn body(response: &[u8]) -> &[u8] {
    let at = response.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    &response[at + 4..]
}
fn assert_error(response: &[u8], expected: u16, code: ErrorCode) {
    assert_eq!(status(response), expected);
    assert_eq!(
        serde_json::from_slice::<ErrorResponse>(body(response))
            .unwrap()
            .error
            .code,
        code
    );
}
