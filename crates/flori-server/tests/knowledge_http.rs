use std::{fmt::Write as _, fs, path::Path, sync::Arc};

use flori_core::{
    ArtifactId, AttemptId, DomainId, ErrorCode, EvidenceEntry, EvidenceId, EvidenceLocator,
    EvidenceManifest, EvidenceManifestSchema, EvidenceView, JobId, PdfRect, PipelineId,
    PipelineRevisionId, PromptSnapshotId, SearchHit, SourceId, TaskId,
};
use flori_store::{
    Store,
    artifact::{NasArtifactStore, retained_artifact_path, task_artifact_path},
};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

#[allow(dead_code)]
#[path = "../src/error.rs"]
mod error;
#[allow(dead_code)]
#[path = "../src/protocol.rs"]
mod protocol;
mod runner {
    use std::sync::Arc;

    use flori_store::Store;

    #[derive(Clone)]
    pub(super) struct HttpState {
        pub(super) store: Arc<Store>,
    }
}
#[path = "../src/knowledge_http.rs"]
mod knowledge_http;

#[tokio::test]
async fn knowledge_http_is_strict_and_returns_only_the_seeded_current_projection() {
    let root = std::env::temp_dir().join(format!("flori-knowledge-http-{}", JobId::generate()));
    fs::create_dir(&root).expect("root");
    let database = root.join("flori.sqlite");
    let store = Arc::new(Store::open(&database).await.expect("store"));
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(&database)
            .foreign_keys(true),
    )
    .await
    .expect("pool");
    let artifact_root = root.join("artifacts");
    let artifacts = NasArtifactStore::new(&artifact_root, 1024 * 1024).expect("artifacts");
    let (job_id, evidence_id) = seed_projection(&store, &artifacts, &pool, &artifact_root).await;

    let listener = TcpListener::bind("localhost:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            knowledge_http::routes().with_state(runner::HttpState { store }),
        )
        .await
        .expect("serve");
    });

    let response = exchange(address, "/api/v1/search?q=declared&limit=10", "").await;
    assert_eq!(status(&response), 200);
    let hits: Vec<SearchHit> = serde_json::from_slice(body(&response)).expect("search response");
    assert_eq!(hits.len(), 2);
    assert!(hits.iter().all(|hit| hit.job_id == job_id));
    assert!(hits.iter().all(|hit| hit.evidence_ids == [evidence_id]));

    let response = exchange(address, &format!("/api/v1/evidence/{evidence_id}"), "").await;
    assert_eq!(status(&response), 200);
    let evidence: EvidenceView = serde_json::from_slice(body(&response)).expect("evidence");
    assert_eq!(evidence.evidence_id, evidence_id);

    for path in [
        "/api/v1/search?q=declared&limit=10&extra=1".to_owned(),
        "/api/v1/search?q=declared".to_owned(),
        "/api/v1/search?q=declared&limit=0".to_owned(),
        "/api/v1/evidence/not-a-uuid".to_owned(),
    ] {
        assert_error(
            &exchange(address, &path, "").await,
            400,
            ErrorCode::InvalidRequest,
        );
    }
    assert_error(
        &exchange(
            address,
            &format!("/api/v1/evidence/{}", EvidenceId::generate()),
            "",
        )
        .await,
        404,
        ErrorCode::NotFound,
    );
    server.abort();
    let _ = server.await;
    pool.close().await;
    fs::remove_dir_all(root).expect("cleanup");
}

async fn seed_projection(
    store: &Store,
    artifacts: &NasArtifactStore,
    pool: &SqlitePool,
    artifact_root: &Path,
) -> (JobId, EvidenceId) {
    let domain_id = DomainId::generate();
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    let source_id = SourceId::generate();
    let job_id = JobId::generate();
    let extract_id = TaskId::generate();
    let note_id = TaskId::generate();
    let validate_id = TaskId::generate();
    let publish_id = TaskId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
        .bind(domain_id.to_string()).bind(format!("d-{domain_id}")).bind("Research").execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
        .bind(pipeline_id.to_string())
        .bind("pdf")
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'pdf: {}',0)")
        .bind(revision_id.to_string()).bind(pipeline_id.to_string()).bind("0".repeat(64)).execute(pool).await.expect("revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload','upload:test','Paper',?,?,?,0,0)")
        .bind(source_id.to_string()).bind(domain_id.to_string()).bind(format!("s-{source_id}")).bind("1".repeat(64)).execute(pool).await.expect("source");
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms) VALUES(?,?,?,'initial','running',?,?,'{}','{\"translate\":false}',?,?,1,1)")
        .bind(job_id.to_string()).bind(source_id.to_string()).bind(revision_id.to_string()).bind(PromptSnapshotId::generate().to_string())
        .bind("2".repeat(64)).bind(format!("j-{job_id}")).bind("3".repeat(64)).execute(pool).await.expect("job");
    for (id, key, executor, state) in [
        (extract_id, "extract", "document.extract", "succeeded"),
        (note_id, "note", "ai.document_note", "succeeded"),
        (validate_id, "validate", "core.validate", "succeeded"),
        (publish_id, "publish", "core.publish", "ready"),
    ] {
        sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms) VALUES(?,?,?,?, '{}','{}',?,1,1000)")
            .bind(id.to_string()).bind(job_id.to_string()).bind(key).bind(executor).bind(state).execute(pool).await.expect("task");
    }
    let source_artifact_id = ArtifactId::generate();
    insert_artifact(
        pool,
        artifact_root,
        source_id,
        job_id,
        extract_id,
        source_artifact_id,
        "source",
        "source_original",
        "application/pdf",
        "source.pdf",
        b"%PDF",
        true,
    )
    .await;
    let evidence_id = EvidenceId::generate();
    let marker = format!("[[evidence:{evidence_id}]]");
    let note = format!("# Note\n\n## 来源事实\n\ndeclared fact {marker}\n\n## AI 分析\n\nanalysis");
    let summary = format!("declared summary {marker}");
    let manifest = EvidenceManifest {
        schema: EvidenceManifestSchema::V1,
        items: vec![EvidenceEntry {
            evidence_id,
            source_artifact_id,
            locator: EvidenceLocator::Pdf {
                page: 1,
                bbox: PdfRect {
                    x1: 1.0,
                    y1: 1.0,
                    x2: 2.0,
                    y2: 2.0,
                },
            },
            quote: "source quote".into(),
        }],
    };
    insert_artifact(
        pool,
        artifact_root,
        source_id,
        job_id,
        note_id,
        ArtifactId::generate(),
        "smart_note",
        "smart_note",
        "text/markdown",
        "note.md",
        note.as_bytes(),
        false,
    )
    .await;
    insert_artifact(
        pool,
        artifact_root,
        source_id,
        job_id,
        note_id,
        ArtifactId::generate(),
        "summary",
        "summary",
        "text/markdown",
        "summary.md",
        summary.as_bytes(),
        false,
    )
    .await;
    insert_artifact(
        pool,
        artifact_root,
        source_id,
        job_id,
        validate_id,
        ArtifactId::generate(),
        "evidence",
        "evidence",
        "application/json",
        "evidence.json",
        &serde_json::to_vec(&manifest).expect("manifest"),
        false,
    )
    .await;
    store
        .publish_job_with_projection(artifacts, job_id, publish_id, AttemptId::generate(), 10)
        .await
        .expect("publish");
    (job_id, evidence_id)
}

#[allow(clippy::too_many_arguments)]
async fn insert_artifact(
    pool: &SqlitePool,
    root: &Path,
    source_id: SourceId,
    job_id: JobId,
    task_id: TaskId,
    artifact_id: ArtifactId,
    name: &str,
    kind: &str,
    media_type: &str,
    file_name: &str,
    bytes: &[u8],
    retained: bool,
) {
    let relative = if retained {
        retained_artifact_path(source_id, artifact_id, file_name)
    } else {
        task_artifact_path(source_id, job_id, task_id, artifact_id, file_name)
    }
    .expect("path");
    let path = root.join(&relative);
    fs::create_dir_all(path.parent().expect("parent")).expect("parents");
    fs::write(path, bytes).expect("bytes");
    sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,'materialized',?,?,?,?,?,?,?,?,0)")
        .bind(artifact_id.to_string()).bind(source_id.to_string()).bind(job_id.to_string()).bind(task_id.to_string())
        .bind(name).bind(kind).bind(media_type).bind(file_name).bind(i64::try_from(bytes.len()).expect("size"))
        .bind(digest(bytes)).bind(relative).bind(if retained { "source" } else { "published" }).execute(pool).await.expect("artifact");
}

fn digest(bytes: &[u8]) -> String {
    let mut value = String::new();
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("digest");
    }
    value
}

async fn exchange(address: std::net::SocketAddr, path: &str, body: &str) -> Vec<u8> {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).await.expect("write");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("read");
    response
}

fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response).expect("http")[9..12]
        .parse()
        .expect("status")
}
fn body(response: &[u8]) -> &[u8] {
    &response[response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("headers")
        + 4..]
}
fn assert_error(response: &[u8], expected_status: u16, code: ErrorCode) {
    assert_eq!(status(response), expected_status);
    let error: flori_core::ErrorResponse = serde_json::from_slice(body(response)).expect("error");
    assert_eq!(error.error.code, code);
}
