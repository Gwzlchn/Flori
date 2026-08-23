use std::{fs, net::SocketAddr, path::PathBuf, sync::Arc, time::Duration};

use flori_core::{
    DomainId, ErrorCode, JobEventPayload, JobId, JobState, JobStateEvent, PipelineId,
    PipelineRevisionId, PromptSnapshotId, SourceChangedEvent, SourceId, SystemView,
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
    job_id: JobId,
}

impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-system-http-{}", JobId::generate()));
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
        store
            .append_event(
                &JobEventPayload::JobState(JobStateEvent {
                    job_id,
                    state: JobState::Succeeded,
                    error_code: None,
                }),
                1,
            )
            .await
            .expect("job event");
        store
            .append_event(
                &JobEventPayload::SourceChanged(SourceChangedEvent { source_id }),
                2,
            )
            .await
            .expect("source event");
        let artifacts =
            Arc::new(NasArtifactStore::new(root.join("artifacts"), 1024).expect("artifact root"));
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let address = listener.local_addr().expect("address");
        let app_store = Arc::clone(&store);
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(
                    app_store,
                    artifacts,
                    "http://localhost/content".into(),
                    60_000,
                )
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
            job_id,
        }
    }

    async fn request(&self, target: &str, extra_headers: &str, marker: &str) -> Vec<u8> {
        let request = format!(
            "GET {target} HTTP/1.1\r\nHost: localhost\r\nX-Flori-Protocol: 1\r\nConnection: close\r\n{extra_headers}\r\n"
        );
        let mut stream = TcpStream::connect(self.address).await.expect("connect");
        stream.write_all(request.as_bytes()).await.expect("request");
        let mut response = Vec::new();
        tokio::time::timeout(Duration::from_secs(3), async {
            let mut buffer = [0_u8; 2048];
            loop {
                let read = stream.read(&mut buffer).await.expect("response");
                if read == 0 {
                    return;
                }
                response.extend_from_slice(&buffer[..read]);
                if !marker.is_empty()
                    && std::str::from_utf8(&response).is_ok_and(|text| text.contains(marker))
                {
                    return;
                }
            }
        })
        .await
        .expect("bounded response");
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
async fn system_and_sse_are_strict_resumable_and_job_scoped() {
    let harness = Harness::new().await;
    let system = harness.request("/api/v1/system", "", "").await;
    assert_eq!(status(&system), 200);
    let view: SystemView = serde_json::from_slice(body(&system)).expect("system body");
    assert_eq!(view.queue_depth, 0);
    assert!(view.disk_free_bytes > 0);

    let global = harness
        .request("/api/v1/events?after=0", "", "event: source_changed")
        .await;
    let global = std::str::from_utf8(&global).expect("SSE UTF-8");
    assert!(global.contains("content-type: text/event-stream"));
    assert!(global.contains("id: 1"));
    assert!(global.contains("event: job_state"));
    assert!(global.contains(&format!(r#""job_id":"{}""#, harness.job_id)));

    let scoped = harness
        .request(
            &format!("/api/v1/jobs/{}/events", harness.job_id),
            "Last-Event-ID: 0\r\n",
            "event: job_state",
        )
        .await;
    let scoped = std::str::from_utf8(&scoped).expect("SSE UTF-8");
    assert!(!scoped.contains("event: source_changed"));

    for (target, headers) in [
        ("/api/v1/events?unknown=1", ""),
        ("/api/v1/events?after=1", "Last-Event-ID: 2\r\n"),
        ("/api/v1/events", "Last-Event-ID: 1\r\nLast-Event-ID: 1\r\n"),
    ] {
        assert_eq!(status(&harness.request(target, headers, "").await), 400);
    }
    assert_eq!(
        status(
            &harness
                .request(
                    &format!("/api/v1/jobs/{}/events", JobId::generate()),
                    "",
                    "",
                )
                .await
        ),
        404
    );

    sqlx::query("DELETE FROM job_events WHERE id<=2")
        .execute(&harness.pool)
        .await
        .expect("expire events");
    let expired = harness.request("/api/v1/events?after=1", "", "").await;
    assert_eq!(status(&expired), 400);
    let error: flori_core::ErrorResponse =
        serde_json::from_slice(body(&expired)).expect("error body");
    assert_eq!(error.error.code, ErrorCode::EventCursorExpired);
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
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms,finished_at_ms) VALUES(?,?,?,'initial','succeeded',?,?,'{}','{\"translate\":false}',?,?,0,0,1)")
        .bind(job.to_string()).bind(source.to_string()).bind(revision.to_string())
        .bind(PromptSnapshotId::generate().to_string()).bind("c".repeat(64))
        .bind(format!("j-{job}")).bind("d".repeat(64)).execute(pool).await.expect("job");
    sqlx::query("UPDATE sources SET current_job_id=? WHERE id=?")
        .bind(job.to_string())
        .bind(source.to_string())
        .execute(pool)
        .await
        .expect("current");
    (source, job)
}

fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response)
        .expect("HTTP UTF-8")
        .split_whitespace()
        .nth(1)
        .expect("status")
        .parse()
        .expect("numeric status")
}

fn body(response: &[u8]) -> &[u8] {
    let marker = b"\r\n\r\n";
    let offset = response
        .windows(marker.len())
        .position(|window| window == marker)
        .expect("headers");
    &response[offset + marker.len()..]
}
