use std::{
    collections::BTreeMap,
    fs,
    path::PathBuf,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use flori_core::{
    AiModelCapability, AiTool, ArtifactKind, ArtifactManifestEntry, AttemptId, CreateRunnerSlot,
    DomainId, Executor, JobId, JobInputs, JobTrigger, LogFrame, PipelineId, PipelineRevisionId,
    PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile, PromptSnapshotPrompt,
    RegisterRunnerRequest, RerunJobRequest, RerunMode, RunnerTool, RunnerToolCapability,
    Sha256Digest, SourceKind, StartUploadRequest, TaskClaim, TaskId, TaskLogLevel, TaskLogLine,
    UsageOrigin, UsageUpdate, VerifyUploadRequest,
};
use flori_pipeline::{Compilation, compile};
use flori_runner::{RunnerClient, manifest_sha256};
use flori_store::{CreateJob, CreateSource, Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{net::TcpListener, task::JoinHandle};

struct Harness {
    root: PathBuf,
    store: Arc<Store>,
    pool: SqlitePool,
    artifacts: Arc<NasArtifactStore>,
    client: RunnerClient,
    compilation: Compilation,
    revision_id: PipelineRevisionId,
    source_id: flori_core::SourceId,
    server: Option<JoinHandle<()>>,
}

impl Harness {
    async fn new() -> (Self, JobId) {
        let root = std::env::temp_dir().join(format!(
            "flori-control-e2e-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&root).expect("test root");
        let database = root.join("flori.sqlite");
        let store = Arc::new(Store::open(&database).await.expect("store"));
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("fixture connection");
        let domain_id = DomainId::generate();
        sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'profile',0,0)")
            .bind(domain_id.to_string()).bind(format!("domain-{domain_id}")).bind("Domain").execute(&pool).await.expect("domain");
        sqlx::query("INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('document_note','note',?,0)")
            .bind(digest(b"note").as_str()).execute(&pool).await.expect("prompt");
        let yaml = include_bytes!("../../../pipelines/pdf.yml");
        let compilation = compile("pdf", yaml).expect("compile pipeline");
        let pipeline_id = PipelineId::generate();
        let revision_id = PipelineRevisionId::generate();
        store
            .register_pipeline_revision(
                pipeline_id,
                revision_id,
                &compilation,
                "test",
                std::str::from_utf8(yaml).expect("pipeline UTF-8"),
                1,
            )
            .await
            .expect("pipeline revision");
        let source_id = store
            .create_source(CreateSource {
                kind: SourceKind::PdfUrl,
                canonical_ref: "https://example.test/paper.pdf",
                title: None,
                domain_id,
                request_key: "source",
                request_sha256: &"a".repeat(64),
                created_at_ms: 2,
            })
            .await
            .expect("source");
        let initial = store
            .create_job(
                CreateJob {
                    source_id,
                    pipeline_revision_id: revision_id,
                    trigger: JobTrigger::Initial,
                    rerun_of_job_id: None,
                    prompt_snapshot_id: PromptSnapshotId::generate(),
                    prompt_snapshot: &snapshot(domain_id),
                    request_key: "initial-job",
                    request_sha256: &"b".repeat(64),
                    inputs: JobInputs { translate: false },
                    created_at_ms: 3,
                },
                &compilation,
            )
            .await
            .expect("initial job");
        store
            .create_runner_slot(
                &CreateRunnerSlot {
                    name: "pdf-ai-runner".into(),
                    tags: vec!["ai".into(), "media".into()],
                    max_concurrency: 1,
                    default_model: Some("model-a".into()),
                    default_effort: Some("high".into()),
                },
                &digest(b"registration"),
                i64::MAX,
                4,
            )
            .await
            .expect("runner slot");
        let artifacts = Arc::new(
            NasArtifactStore::new(root.join("artifacts"), 128 * 1024 * 1024).expect("NAS"),
        );
        let listener = TcpListener::bind("localhost:0").await.expect("bind server");
        let address = listener.local_addr().expect("server address");
        let server_store = Arc::clone(&store);
        let server_artifacts = Arc::clone(&artifacts);
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(
                    server_store,
                    server_artifacts,
                    "http://localhost/content".into(),
                    60_000,
                )
                .expect("app"),
            )
            .await
            .expect("serve");
        });
        let base = format!("http://{address}");
        let registered = RunnerClient::register(&base, "registration", &capabilities())
            .await
            .expect("register");
        let client = RunnerClient::new(&base, registered.token).expect("client");
        (
            Self {
                root,
                store,
                pool,
                artifacts,
                client,
                compilation,
                revision_id,
                source_id,
                server: Some(server),
            },
            initial,
        )
    }

    async fn close(mut self) {
        let server = self.server.take().expect("server handle");
        server.abort();
        let _ = server.await;
        self.pool.close().await;
        let root = self.root.clone();
        drop(self);
        fs::remove_dir_all(root).expect("remove fixture");
    }
}

fn snapshot(domain_id: DomainId) -> PromptSnapshot {
    PromptSnapshot {
        profile: PromptSnapshotProfile {
            domain_id,
            profile_text: "profile".into(),
            sha256: digest(b"profile"),
        },
        prompts: vec![PromptSnapshotPrompt {
            key: "document_note".into(),
            content: "note".into(),
            sha256: digest(b"note"),
        }],
    }
}

fn capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![
            RunnerToolCapability {
                tool: RunnerTool::PdfExtractor,
                version: "1".into(),
            },
            RunnerToolCapability {
                tool: RunnerTool::CodexCli,
                version: "1".into(),
            },
        ],
        ai_models: vec![AiModelCapability {
            model: "model-a".into(),
            efforts: vec!["high".into()],
        }],
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

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system time")
        .as_millis()
        .try_into()
        .expect("timestamp")
}

async fn upload(
    client: &RunnerClient,
    claim: &TaskClaim,
    name: &str,
    media: &str,
    bytes: &[u8],
) -> ArtifactManifestEntry {
    let request = StartUploadRequest {
        name: name.into(),
        media_type: media.into(),
        size_bytes: bytes.len() as u64,
        sha256: digest(bytes),
    };
    let started = client
        .start_upload(claim.exec_id, &request)
        .await
        .expect("start upload");
    client
        .append_upload_chunk(started.upload_id, started.received_bytes, bytes.to_vec())
        .await
        .expect("upload bytes");
    client
        .verify_upload(
            started.upload_id,
            &VerifyUploadRequest {
                size_bytes: request.size_bytes,
                sha256: request.sha256,
            },
        )
        .await
        .expect("verify")
        .artifact
}

fn artifact_body(kind: ArtifactKind) -> (&'static str, &'static [u8]) {
    match kind {
        ArtifactKind::SourceOriginal => ("application/pdf", b"%PDF-1.7\n"),
        ArtifactKind::DocumentStructure => (
            "application/json",
            br#"{"schema":"flori.document_structure.v1","pages":[]}"#,
        ),
        ArtifactKind::SmartNote => ("text/markdown", b"# Smart note\n"),
        ArtifactKind::Summary => ("text/markdown", b"# Summary\n"),
        ArtifactKind::Terms => (
            "application/json",
            br#"{"schema":"flori.terms.v1","terms":[]}"#,
        ),
        ArtifactKind::AiAudit => ("application/json", br#"{"tool":"codex_cli","status":"ok"}"#),
        _ => panic!("unexpected required runner artifact: {kind:?}"),
    }
}

async fn run_runner_task(client: &RunnerClient, claim: &TaskClaim) {
    let line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: 1,
        level: TaskLogLevel::Info,
        message: format!("{} complete", claim.task_key),
    })
    .expect("log line");
    client
        .append_logs(
            claim.exec_id,
            &[LogFrame {
                sequence: 1,
                sha256: digest(line.as_bytes()),
                line,
            }],
        )
        .await
        .expect("append log");
    if claim.executor == Executor::AiDocumentNote {
        assert_eq!(
            (claim.model.as_deref(), claim.effort.as_deref()),
            (Some("model-a"), Some("high"))
        );
        let started = client
            .update_usage(
                claim.exec_id,
                &UsageUpdate::Started {
                    invocation_key: "note-call".into(),
                    tool: AiTool::CodexCli,
                    model: "model-a".into(),
                    effort: "high".into(),
                },
            )
            .await
            .expect("usage start");
        let finalized = client
            .update_usage(
                claim.exec_id,
                &UsageUpdate::Final {
                    invocation_key: "note-call".into(),
                    origin: UsageOrigin::Observed,
                    input_tokens: Some(10),
                    output_tokens: Some(20),
                    cost_micros: None,
                    credits_micros: Some(1),
                },
            )
            .await
            .expect("usage final");
        assert_eq!(started.usage_id, finalized.usage_id);
    }
    let mut entries = Vec::new();
    for output in claim
        .output_declarations
        .iter()
        .filter(|output| output.required && output.kind != ArtifactKind::TaskLog)
    {
        let (media, bytes) = artifact_body(output.kind);
        entries.push(upload(client, claim, &output.name, media, bytes).await);
    }
    let manifest =
        manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, entries).expect("manifest");
    client
        .complete(claim.exec_id, manifest)
        .await
        .expect("complete");
}

async fn execute_and_publish(harness: &Harness, job_id: JobId) {
    for expected in ["acquire", "extract", "note"] {
        let claim = harness.client.poll().await.expect("poll").expect("claim");
        assert_eq!((claim.job_id, claim.task_key.as_str()), (job_id, expected));
        run_runner_task(&harness.client, &claim).await;
    }
    assert!(
        harness
            .client
            .poll()
            .await
            .expect("core tasks are not polled")
            .is_none()
    );
    let rows: Vec<(String, String)> = sqlx::query_as(
        "SELECT task_key,id FROM tasks WHERE job_id=? AND task_key IN ('validate','publish')",
    )
    .bind(job_id.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("core tasks");
    let ids = rows.into_iter().collect::<BTreeMap<_, _>>();
    let now = now_ms();
    harness
        .store
        .complete_core_task(
            job_id,
            ids["validate"].parse::<TaskId>().expect("validate ID"),
            AttemptId::generate(),
            now,
        )
        .await
        .expect("validate");
    harness
        .store
        .publish_job(
            job_id,
            ids["publish"].parse::<TaskId>().expect("publish ID"),
            AttemptId::generate(),
            now + 1,
        )
        .await
        .expect("publish");
}

async fn task_ids(pool: &SqlitePool, job_id: JobId) -> BTreeMap<String, String> {
    sqlx::query_as("SELECT task_key,id FROM tasks WHERE job_id=? ORDER BY task_key")
        .bind(job_id.to_string())
        .fetch_all(pool)
        .await
        .expect("task IDs")
        .into_iter()
        .collect()
}

async fn assert_published(harness: &Harness, job_id: JobId, usage_total: i64) {
    let state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
        .bind(job_id.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("job state");
    assert_eq!(state, "succeeded");
    let names: Vec<String> = sqlx::query_scalar("SELECT t.task_key||'/'||a.name||':'||a.kind FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? ORDER BY 1")
        .bind(job_id.to_string()).fetch_all(&harness.pool).await.expect("artifact names");
    assert_eq!(
        names,
        [
            "acquire/log:task_log",
            "acquire/original:source_original",
            "extract/log:task_log",
            "extract/structure:document_structure",
            "note/audit:ai_audit",
            "note/log:task_log",
            "note/smart_note:smart_note",
            "note/summary:summary",
            "note/terms:terms"
        ]
        .map(str::to_owned)
    );
    let artifacts: Vec<(String, String)> = sqlx::query_as(
        "SELECT kind,relative_path FROM artifacts WHERE job_id=? ORDER BY kind,relative_path",
    )
    .bind(job_id.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("artifacts");
    assert_eq!(artifacts.len(), 9);
    assert_eq!(
        artifacts
            .iter()
            .filter(|(kind, _)| kind == "task_log")
            .count(),
        3
    );
    for (kind, relative) in artifacts {
        let bytes =
            fs::read(harness.root.join("artifacts").join(relative)).expect("artifact bytes");
        if kind == "task_log" {
            serde_json::from_str::<TaskLogLine>(
                std::str::from_utf8(&bytes).expect("UTF-8 log").trim_end(),
            )
            .expect("strict persisted log");
        }
    }
    let usage: (i64, i64) = sqlx::query_as("SELECT count(*),sum(state='final') FROM ai_usage")
        .fetch_one(&harness.pool)
        .await
        .expect("usage");
    assert_eq!(usage, (usage_total, usage_total));
}

#[tokio::test]
async fn pdf_control_plane_executes_and_reruns_over_real_http_sqlite_and_nas() {
    let (harness, first) = Harness::new().await;
    let first_ids = task_ids(&harness.pool, first).await;
    execute_and_publish(&harness, first).await;
    assert_published(&harness, first, 1).await;
    let domain_id = snapshot_domain(&harness.pool).await;
    let second = harness
        .store
        .create_job(
            CreateJob {
                source_id: harness.source_id,
                pipeline_revision_id: harness.revision_id,
                trigger: JobTrigger::PipelineRerun,
                rerun_of_job_id: Some(first),
                prompt_snapshot_id: PromptSnapshotId::generate(),
                prompt_snapshot: &snapshot(domain_id),
                request_key: "pipeline-rerun",
                request_sha256: &"c".repeat(64),
                inputs: JobInputs { translate: false },
                created_at_ms: now_ms(),
            },
            &harness.compilation,
        )
        .await
        .expect("pipeline rerun");
    let second_ids = task_ids(&harness.pool, second).await;
    assert!(
        first_ids
            .values()
            .all(|id| !second_ids.values().any(|new| new == id))
    );
    execute_and_publish(&harness, second).await;
    assert_published(&harness, second, 2).await;
    let pointers: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("publication pointers");
    assert_eq!(pointers, (second.to_string(), first.to_string()));
    let from_note = harness
        .store
        .rerun_from_task(
            &harness.artifacts,
            second,
            &RerunJobRequest {
                request_key: "note-rerun".into(),
                mode: RerunMode::FromTask,
                from_task_key: Some("note".into()),
                ai_selection: None,
            },
            &harness.compilation,
            now_ms(),
        )
        .await
        .expect("from task rerun");
    let states: BTreeMap<String, String> =
        sqlx::query_as("SELECT task_key,state FROM tasks WHERE job_id=?")
            .bind(from_note.to_string())
            .fetch_all(&harness.pool)
            .await
            .expect("rerun states")
            .into_iter()
            .collect();
    assert_eq!(
        (&states["extract"], &states["note"], &states["validate"]),
        (&"skipped".into(), &"ready".into(), &"pending".into())
    );
    let materialized: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM artifacts WHERE job_id=? AND origin='materialized'",
    )
    .bind(from_note.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("materialized artifact");
    assert_eq!(materialized, 2);
    harness.close().await;
}

async fn snapshot_domain(pool: &SqlitePool) -> DomainId {
    sqlx::query_scalar::<_, String>("SELECT id FROM domains LIMIT 1")
        .fetch_one(pool)
        .await
        .expect("domain ID")
        .parse()
        .expect("typed domain ID")
}
