use std::{fs, net::SocketAddr, path::PathBuf, sync::Arc};

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactWhen, AttemptId, CompiledTaskSpec,
    DomainId, ErrorCode, ErrorResponse, Executor, JobId, PipelineId, PipelineRevisionId,
    PromptSnapshotId, RunnerId, SourceId, TaskId,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    task::JoinHandle,
};

pub(super) struct Harness {
    root: PathBuf,
    pub(super) pool: SqlitePool,
    address: SocketAddr,
    server: JoinHandle<()>,
    pub(super) source_id: SourceId,
    pub(super) current_job_id: JobId,
    pub(super) previous_job_id: JobId,
    pub(super) note_task_id: TaskId,
    pub(super) current_attempt_id: AttemptId,
    pub(super) runner_id: RunnerId,
}

impl Harness {
    pub(super) async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-detail-{}", JobId::generate()));
        fs::create_dir(&root).expect("test root");
        let database = root.join("flori.sqlite");
        let store = Arc::new(Store::open(&database).await.expect("empty SQLite"));
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("inspection pool");
        let seed = seed(&pool).await;
        let artifacts = Arc::new(
            NasArtifactStore::new(root.join("artifacts"), 1024 * 1024).expect("artifact root"),
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
            pool,
            address,
            server,
            source_id: seed.source_id,
            current_job_id: seed.current_job_id,
            previous_job_id: seed.previous_job_id,
            note_task_id: seed.note_task_id,
            current_attempt_id: seed.current_attempt_id,
            runner_id: seed.runner_id,
        }
    }

    pub(super) async fn get(&self, path: &str) -> Vec<u8> {
        let request = format!(
            "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\
             X-Flori-Protocol: 1\r\n\r\n"
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

struct Seed {
    source_id: SourceId,
    current_job_id: JobId,
    previous_job_id: JobId,
    note_task_id: TaskId,
    current_attempt_id: AttemptId,
    runner_id: RunnerId,
}

async fn seed(pool: &SqlitePool) -> Seed {
    let domain_id = DomainId::generate();
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    let source_id = SourceId::generate();
    let previous_job_id = JobId::generate();
    let current_job_id = JobId::generate();
    let runner_id = RunnerId::generate();
    let acquire_task_id = TaskId::generate();
    let note_task_id = TaskId::generate();
    let failed_attempt_id = AttemptId::generate();
    let current_attempt_id = AttemptId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
        .bind(domain_id.to_string()).bind(format!("d-{domain_id}")).bind("Research").execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,'pdf',0)")
        .bind(pipeline_id.to_string())
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'pdf: {}',0)")
        .bind(revision_id.to_string()).bind(pipeline_id.to_string()).bind("0".repeat(64)).execute(pool).await.expect("revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload','upload:paper','Paper',?,?,?,0,0)")
        .bind(source_id.to_string()).bind(domain_id.to_string()).bind(format!("s-{source_id}")).bind("1".repeat(64)).execute(pool).await.expect("source");
    for (job_id, trigger, key) in [
        (previous_job_id, "pipeline_rerun", "previous"),
        (current_job_id, "initial", "current"),
    ] {
        sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms,finished_at_ms) VALUES(?,?,?,?,'succeeded',?,?,'{}','{\"translate\":false}',?,?,0,0,1)")
            .bind(job_id.to_string()).bind(source_id.to_string()).bind(revision_id.to_string()).bind(trigger)
            .bind(PromptSnapshotId::generate().to_string()).bind("2".repeat(64)).bind(key).bind("3".repeat(64)).execute(pool).await.expect("job");
    }
    sqlx::query("UPDATE sources SET current_job_id=?,previous_job_id=? WHERE id=?")
        .bind(current_job_id.to_string())
        .bind(previous_job_id.to_string())
        .bind(source_id.to_string())
        .execute(pool)
        .await
        .expect("published pointers");
    sqlx::query("INSERT INTO runners(id,name,state,config_revision,max_concurrency,tags_json,tools_json,ai_models_json,default_model,default_effort,created_at_ms,updated_at_ms) VALUES(?,'ai-one','enabled',7,1,'[\"ai\"]','[]','[]','model-a','high',0,0)")
        .bind(runner_id.to_string()).execute(pool).await.expect("runner");
    let acquire = spec(
        Executor::DocumentAcquire,
        vec![],
        "source",
        ArtifactKind::SourceOriginal,
    );
    let note = spec(
        Executor::AiDocumentNote,
        vec!["acquire".into()],
        "smart_note",
        ArtifactKind::SmartNote,
    );
    for (id, key, executor, spec, attempts, runner) in [
        (
            note_task_id,
            "note",
            "ai.document_note",
            note,
            2_i64,
            Some(runner_id),
        ),
        (
            acquire_task_id,
            "acquire",
            "document.acquire",
            acquire,
            1_i64,
            None,
        ),
    ] {
        sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,pinned_runner_id,selected_model,selected_effort,runner_config_revision,attempt_limit,timeout_ms) VALUES(?,?,?,?,?,'{}','succeeded',?,'model-a','high',7,?,1000)")
            .bind(id.to_string()).bind(current_job_id.to_string()).bind(key).bind(executor)
            .bind(serde_json::to_string(&spec).expect("spec")).bind(runner.map(|id| id.to_string())).bind(attempts).execute(pool).await.expect("task");
    }
    for (id, number, state) in [
        (current_attempt_id, 2_i64, "succeeded"),
        (failed_attempt_id, 1_i64, "failed"),
    ] {
        sqlx::query("INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,model,effort,runner_config_revision,lease_expires_at_ms,last_log_sequence,started_at_ms,finished_at_ms) VALUES(?,?,?,?,?,'model-a','high',7,10,2,1,2)")
            .bind(id.to_string()).bind(note_task_id.to_string()).bind(number).bind(runner_id.to_string()).bind(state).execute(pool).await.expect("attempt");
    }
    sqlx::query("UPDATE tasks SET current_attempt_id=? WHERE id=?")
        .bind(current_attempt_id.to_string())
        .bind(note_task_id.to_string())
        .execute(pool)
        .await
        .expect("current attempt");
    for (task_id, name, kind) in [
        (note_task_id, "smart_note", "smart_note"),
        (acquire_task_id, "source", "source_original"),
    ] {
        sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,'materialized',?,?, 'application/octet-stream','fixture',1,?,'fixture','published',1)")
            .bind(ArtifactId::generate().to_string()).bind(source_id.to_string()).bind(current_job_id.to_string()).bind(task_id.to_string())
            .bind(name).bind(kind).bind("4".repeat(64)).execute(pool).await.expect("artifact");
    }
    Seed {
        source_id,
        current_job_id,
        previous_job_id,
        note_task_id,
        current_attempt_id,
        runner_id,
    }
}

fn spec(
    executor: Executor,
    needs: Vec<String>,
    name: &str,
    kind: ArtifactKind,
) -> CompiledTaskSpec {
    CompiledTaskSpec {
        executor,
        needs,
        tags: vec!["ai".into()],
        retry: u8::from(executor == Executor::AiDocumentNote),
        timeout_ms: 1000,
        artifacts: vec![ArtifactDeclaration {
            name: name.into(),
            kind,
            path: "fixture".into(),
            required: true,
            when: ArtifactWhen::OnSuccess,
            max_files: Some(1),
            max_bytes: 1024,
        }],
    }
}

pub(super) fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(
        response
            .split(|byte| *byte == b'\n')
            .next()
            .expect("status"),
    )
    .expect("UTF-8 status")
    .split_whitespace()
    .nth(1)
    .expect("status code")
    .parse()
    .expect("numeric status")
}
pub(super) fn body(response: &[u8]) -> &[u8] {
    let start = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("body");
    &response[start + 4..]
}
pub(super) fn assert_error(response: &[u8], expected_status: u16, expected_code: ErrorCode) {
    assert_eq!(status(response), expected_status);
    let error: ErrorResponse = serde_json::from_slice(body(response)).expect("error response");
    assert_eq!(error.error.code, expected_code);
}
