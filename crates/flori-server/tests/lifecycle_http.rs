use std::{fs, net::SocketAddr, path::PathBuf, sync::Arc};

use flori_core::{
    DomainId, ErrorCode, JobId, PipelineId, PipelineRevisionId, PromptSnapshotId, SourceId,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};

struct Harness {
    root: PathBuf,
    pool: SqlitePool,
    address: SocketAddr,
    server: JoinHandle<()>,
    source_id: SourceId,
    job_id: JobId,
}

impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-lifecycle-http-{}", JobId::generate()));
        fs::create_dir(&root).expect("fixture root");
        let database = root.join("flori.sqlite");
        let store = Arc::new(Store::open(&database).await.expect("store"));
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let (source_id, job_id) = seed(&pool).await;
        let artifacts =
            Arc::new(NasArtifactStore::new(root.join("artifacts"), 1024).expect("artifact root"));
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
            pool,
            address,
            server,
            source_id,
            job_id,
        }
    }

    async fn request(&self, method: &str, path: &str) -> Vec<u8> {
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nX-Flori-Protocol: 1\r\nContent-Length: 0\r\n\r\n"
        );
        let mut stream = TcpStream::connect(self.address).await.expect("connect");
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

#[tokio::test]
async fn cancel_then_delete_is_strict_and_idempotent_over_http() {
    let harness = Harness::new().await;
    assert_eq!(
        status(
            &harness
                .request("POST", &format!("/api/v1/jobs/{}/cancel", harness.job_id))
                .await
        ),
        204
    );
    assert_eq!(
        status(
            &harness
                .request("POST", &format!("/api/v1/jobs/{}/cancel", harness.job_id))
                .await
        ),
        204
    );
    assert_eq!(
        status(
            &harness
                .request("DELETE", &format!("/api/v1/sources/{}", harness.source_id))
                .await
        ),
        204
    );
    assert_eq!(
        status(
            &harness
                .request("DELETE", &format!("/api/v1/sources/{}", harness.source_id))
                .await
        ),
        204
    );
    let remaining: i64 =
        sqlx::query_scalar("SELECT (SELECT count(*) FROM sources)+(SELECT count(*) FROM jobs)")
            .fetch_one(&harness.pool)
            .await
            .expect("remaining rows");
    assert_eq!(remaining, 0);
    assert_eq!(
        status(
            &harness
                .request("POST", "/api/v1/jobs/not-a-uuid/cancel")
                .await
        ),
        400
    );
    let missing = JobId::generate();
    assert_error(
        &harness
            .request("POST", &format!("/api/v1/jobs/{missing}/cancel"))
            .await,
        ErrorCode::NotFound,
    );
}

async fn seed(pool: &SqlitePool) -> (SourceId, JobId) {
    let domain = DomainId::generate();
    let pipeline = PipelineId::generate();
    let revision = PipelineRevisionId::generate();
    let source = SourceId::generate();
    let job = JobId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
        .bind(domain.to_string()).bind(format!("d-{domain}")).bind("domain").execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
        .bind(pipeline.to_string())
        .bind(format!("p-{pipeline}"))
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'tasks: {}',0)")
        .bind(revision.to_string()).bind(pipeline.to_string()).bind("a".repeat(64)).execute(pool).await.expect("revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload',?,?,?,?,0,0)")
        .bind(source.to_string()).bind(format!("upload:{source}")).bind(domain.to_string())
        .bind(format!("s-{source}")).bind("b".repeat(64)).execute(pool).await.expect("source");
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms) VALUES(?,?,?,'initial','queued',?,?,'{}','{\"translate\":false}',?,?,0)")
        .bind(job.to_string()).bind(source.to_string()).bind(revision.to_string())
        .bind(PromptSnapshotId::generate().to_string()).bind("c".repeat(64))
        .bind(format!("j-{job}")).bind("d".repeat(64)).execute(pool).await.expect("job");
    (source, job)
}

fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(
        response
            .split(|byte| *byte == b'\n')
            .next()
            .expect("status"),
    )
    .expect("UTF-8")
    .split_whitespace()
    .nth(1)
    .expect("code")
    .parse()
    .expect("numeric")
}

fn assert_error(response: &[u8], code: ErrorCode) {
    assert_eq!(status(response), 404);
    let body = response
        .windows(4)
        .position(|part| part == b"\r\n\r\n")
        .expect("body");
    let error: flori_core::ErrorResponse =
        serde_json::from_slice(&response[body + 4..]).expect("error");
    assert_eq!(error.error.code, code);
}
