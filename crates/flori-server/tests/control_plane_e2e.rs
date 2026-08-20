use std::{
    collections::BTreeMap,
    fs,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{
    AiAudit, AiModelCapability, AiTool, ArtifactKind, ArtifactManifestEntry, AttemptId,
    CreateRunnerSlot, DomainId, ErrorCode, Executor, JobId, JobInputs, JobTrigger, LogFrame,
    PipelineId, PipelineRevisionId, PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile,
    PromptSnapshotPrompt, RegisterRunnerRequest, RerunJobRequest, RerunMode, ResolvedTaskInputs,
    RunnerTool, RunnerToolCapability, Sha256Digest, SourceInputId, SourceKind, StartUploadRequest,
    TaskClaim, TaskId, TaskLogLevel, TaskLogLine, UsageOrigin, UsageUpdate, VerifyUploadRequest,
};
use flori_pipeline::{Compilation, compile};
use flori_runner::{DaemonConfig, RunnerClient, manifest_sha256, run_ai_daemon};
use flori_store::{
    CreateJob, CreateSource, Store,
    artifact::{NasArtifactStore, source_input_path},
};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
    sync::watch,
    task::JoinHandle,
};

struct Harness {
    root: PathBuf,
    store: Arc<Store>,
    pool: SqlitePool,
    artifacts: Arc<NasArtifactStore>,
    client: RunnerClient,
    compilation: Compilation,
    revision_id: PipelineRevisionId,
    source_id: flori_core::SourceId,
    address: std::net::SocketAddr,
    runner_token: String,
    other_runner_token: String,
    source_input_id: SourceInputId,
    source_input_path: String,
    wrong_source_input_id: SourceInputId,
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
        let artifacts = Arc::new(
            NasArtifactStore::new(root.join("artifacts"), 128 * 1024 * 1024).expect("NAS"),
        );
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
        let input_bytes = b"%PDF input\n";
        let source_input_id = SourceInputId::generate();
        let source_relative_path =
            source_input_path(source_id, source_input_id, "input.pdf").expect("source input path");
        write_artifact(&root, &source_relative_path, input_bytes);
        sqlx::query(
            "INSERT INTO source_inputs(id,source_id,name,media_type,size_bytes,sha256, \
             relative_path,created_at_ms) VALUES(?,?,'input.pdf','application/pdf',?,?,?,2)",
        )
        .bind(source_input_id.to_string())
        .bind(source_id.to_string())
        .bind(i64::try_from(input_bytes.len()).expect("input size"))
        .bind(digest(input_bytes).as_str())
        .bind(&source_relative_path)
        .execute(&pool)
        .await
        .expect("source input");
        let wrong_source = store
            .create_source(CreateSource {
                kind: SourceKind::PdfUpload,
                canonical_ref: "wrong-source",
                title: None,
                domain_id,
                request_key: "wrong-source",
                request_sha256: &"e".repeat(64),
                created_at_ms: 2,
            })
            .await
            .expect("wrong source");
        let wrong_source_input_id = SourceInputId::generate();
        let wrong_path = source_input_path(wrong_source, wrong_source_input_id, "wrong.pdf")
            .expect("wrong source input path");
        write_artifact(&root, &wrong_path, b"wrong");
        sqlx::query(
            "INSERT INTO source_inputs(id,source_id,name,media_type,size_bytes,sha256, \
             relative_path,created_at_ms) VALUES(?,?,'wrong.pdf','application/pdf',5,?,?,2)",
        )
        .bind(wrong_source_input_id.to_string())
        .bind(wrong_source.to_string())
        .bind(digest(b"wrong").as_str())
        .bind(wrong_path)
        .execute(&pool)
        .await
        .expect("wrong source input");
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
        store
            .create_runner_slot(
                &CreateRunnerSlot {
                    name: "other-runner".into(),
                    tags: vec!["ai".into(), "media".into()],
                    max_concurrency: 1,
                    default_model: Some("model-a".into()),
                    default_effort: Some("high".into()),
                },
                &digest(b"other-registration"),
                i64::MAX,
                4,
            )
            .await
            .expect("other runner slot");
        let listener = TcpListener::bind("localhost:0").await.expect("bind server");
        let address = listener.local_addr().expect("server address");
        let base = format!("http://{address}");
        let download_base = base.clone();
        let server_store = Arc::clone(&store);
        let server_artifacts = Arc::clone(&artifacts);
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(server_store, server_artifacts, download_base, 60_000)
                    .expect("app"),
            )
            .await
            .expect("serve");
        });
        let registered = RunnerClient::register(&base, "registration", &capabilities())
            .await
            .expect("register");
        let runner_token = registered.token;
        let client = RunnerClient::new(&base, runner_token.clone()).expect("client");
        let other_runner_token =
            RunnerClient::register(&base, "other-registration", &capabilities())
                .await
                .expect("register other runner")
                .token;
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
                address,
                runner_token,
                other_runner_token,
                source_input_id,
                source_input_path: source_relative_path,
                wrong_source_input_id,
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

fn write_artifact(root: &std::path::Path, relative: &str, bytes: &[u8]) {
    let path = root.join("artifacts").join(relative);
    fs::create_dir_all(path.parent().expect("artifact parent")).expect("artifact directory");
    fs::write(path, bytes).expect("artifact bytes");
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
                    credits_micros: None,
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
        if expected == "acquire" {
            assert_source_content(harness, &claim).await;
        } else if expected == "extract" {
            assert_artifact_content(harness, &claim).await;
        }
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
    validate_and_publish(harness, job_id).await;
}

async fn validate_and_publish(harness: &Harness, job_id: JobId) {
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

async fn assert_source_content(harness: &Harness, claim: &TaskClaim) {
    let ResolvedTaskInputs::DocumentAcquire { source } = &claim.resolved_inputs else {
        panic!("acquire inputs");
    };
    assert_eq!(
        source.input.as_ref().map(|input| input.source_input_id),
        Some(harness.source_input_id)
    );
    let path = format!("/api/v1/source-inputs/{}/content", harness.source_input_id);
    let partial = content_get(harness, &path, &harness.runner_token, Some("bytes=1-4")).await;
    assert_eq!((status(&partial), body(&partial)), (206, &b"PDF "[..]));
    assert_eq!(header(&partial, "content-range"), Some("bytes 1-4/11"));

    assert_error(
        &content_get(harness, &path, &harness.other_runner_token, None).await,
        404,
        ErrorCode::NotFound,
    );
    let wrong = format!(
        "/api/v1/source-inputs/{}/content",
        harness.wrong_source_input_id
    );
    assert_error(
        &content_get(harness, &wrong, &harness.runner_token, None).await,
        404,
        ErrorCode::NotFound,
    );
    let invalid = content_get(harness, &path, &harness.runner_token, Some("bytes=99-")).await;
    assert_eq!(status(&invalid), 416);
    assert_eq!(header(&invalid, "content-range"), Some("bytes */11"));

    sqlx::query("UPDATE attempts SET lease_expires_at_ms=0 WHERE id=?")
        .bind(claim.exec_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("expire lease");
    assert_error(
        &content_get(harness, &path, &harness.runner_token, None).await,
        404,
        ErrorCode::NotFound,
    );
    sqlx::query("UPDATE attempts SET lease_expires_at_ms=? WHERE id=?")
        .bind(claim.lease_expires_at_ms)
        .bind(claim.exec_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("restore lease");
    sqlx::query("UPDATE tasks SET current_attempt_id=NULL WHERE id=?")
        .bind(claim.task_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("clear current attempt");
    assert_error(
        &content_get(harness, &path, &harness.runner_token, None).await,
        404,
        ErrorCode::NotFound,
    );
    sqlx::query("UPDATE tasks SET current_attempt_id=? WHERE id=?")
        .bind(claim.exec_id.to_string())
        .bind(claim.task_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("restore current attempt");

    let file = harness
        .root
        .join("artifacts")
        .join(&harness.source_input_path);
    fs::write(&file, b"size drift!").expect("size drift");
    assert_error(
        &content_get(harness, &path, &harness.runner_token, Some("bytes=0-0")).await,
        400,
        ErrorCode::DigestMismatch,
    );
    fs::write(&file, b"same length").expect("digest drift");
    assert_error(
        &content_get(harness, &path, &harness.runner_token, None).await,
        400,
        ErrorCode::DigestMismatch,
    );
    fs::write(&file, b"%PDF input\n").expect("restore input");
    #[cfg(unix)]
    {
        fs::remove_file(&file).expect("remove input");
        std::os::unix::fs::symlink(harness.root.join("flori.sqlite"), &file).expect("symlink");
        assert_error(
            &content_get(harness, &path, &harness.runner_token, None).await,
            400,
            ErrorCode::ArtifactInvalidPath,
        );
        fs::remove_file(&file).expect("remove symlink");
        fs::write(file, b"%PDF input\n").expect("restore input");
    }
}

async fn assert_artifact_content(harness: &Harness, claim: &TaskClaim) {
    let ResolvedTaskInputs::DocumentExtract { pdf } = &claim.resolved_inputs else {
        panic!("extract inputs");
    };
    let path = format!("/api/v1/artifacts/{}/content", pdf.artifact_id);
    let response = content_get(harness, &path, &harness.runner_token, None).await;
    assert_eq!(
        (status(&response), body(&response)),
        (200, &b"%PDF-1.7\n"[..])
    );
    assert_eq!(header(&response, "accept-ranges"), Some("bytes"));
    assert_eq!(header(&response, "content-type"), Some("application/pdf"));
    let undeclared: String = sqlx::query_scalar(
        "SELECT a.id FROM artifacts a JOIN tasks t ON t.id=a.task_id \
         WHERE a.job_id=? AND t.task_key='acquire' AND a.kind='task_log'",
    )
    .bind(claim.job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("undeclared artifact");
    assert_error(
        &content_get(
            harness,
            &format!("/api/v1/artifacts/{undeclared}/content"),
            &harness.runner_token,
            None,
        )
        .await,
        404,
        ErrorCode::NotFound,
    );
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
async fn codex_daemon_publishes_over_real_http_sqlite_and_nas() {
    let (harness, job_id) = Harness::new().await;
    for expected in ["acquire", "extract"] {
        let claim = harness.client.poll().await.expect("poll").expect("claim");
        assert_eq!((claim.job_id, claim.task_key.as_str()), (job_id, expected));
        if expected == "acquire" {
            assert_source_content(&harness, &claim).await;
        } else {
            assert_artifact_content(&harness, &claim).await;
        }
        run_runner_task(&harness.client, &claim).await;
    }

    let envelope = r#"{"executor":"ai.document_note","schema":"flori.ai_result.v1","smart_note_markdown":"AI note","summary_markdown":"AI summary","terms":{"schema":"flori.terms.v1","terms":[]}}"#;
    let agent = format!(
        r#"{{"type":"item.completed","item":{{"id":"item","type":"agent_message","text":{}}}}}"#,
        serde_json::to_string(envelope).expect("nested result")
    );
    let events = [
        r#"{"type":"thread.started","thread_id":"thread"}"#.to_owned(),
        r#"{"type":"turn.started"}"#.to_owned(),
        agent,
        r#"{"type":"turn.completed","usage":{"input_tokens":41,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":17,"reasoning_output_tokens":1}}"#.to_owned(),
    ];
    let executable = harness.root.join("fake-codex");
    let captured_argv = harness.root.join("fake-codex.argv");
    let captured_stdin = harness.root.join("fake-codex.stdin");
    let event_writes = events
        .iter()
        .map(|event| format!("printf '%s\\n' '{event}'"))
        .collect::<Vec<_>>()
        .join("\n");
    let script = format!(
        "#!/bin/sh\nresult=''\nprintf '%s\\n' \"$@\" > '{argv}'\n\
         while [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--output-last-message' ]; then shift; result=$1; fi\n  shift\ndone\n\
         cat > '{stdin}'\nprintf '%s' '{envelope}' > \"$result\"\n{event_writes}\n",
        argv = captured_argv.display(),
        stdin = captured_stdin.display(),
    );
    fs::write(&executable, script).expect("fake Codex");
    let mut permissions = fs::metadata(&executable)
        .expect("fake metadata")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&executable, permissions).expect("fake executable");
    for directory in ["daemon-home", "daemon-config", "daemon-work"] {
        fs::create_dir(harness.root.join(directory)).expect("daemon directory");
    }
    let config = DaemonConfig {
        tool: AiTool::CodexCli,
        executable,
        home: harness.root.join("daemon-home"),
        tool_config_home: harness.root.join("daemon-config"),
        work_root: harness.root.join("daemon-work"),
        model: "model-a".into(),
        effort: "high".into(),
        renew_interval: Duration::from_millis(100),
        max_output_bytes: 1024 * 1024,
    };
    let daemon_client = RunnerClient::new(
        &format!("http://{}", harness.address),
        harness.runner_token.clone(),
    )
    .expect("daemon client");
    let (cancel_tx, mut cancel_rx) = watch::channel(false);
    let daemon =
        tokio::spawn(async move { run_ai_daemon(&daemon_client, &config, &mut cancel_rx).await });
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let state: String =
                sqlx::query_scalar("SELECT state FROM tasks WHERE job_id=? AND task_key='note'")
                    .bind(job_id.to_string())
                    .fetch_one(&harness.pool)
                    .await
                    .expect("note state");
            if state == "succeeded" {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("AI daemon completion");
    cancel_tx.send(true).expect("cancel daemon");
    assert_eq!(daemon.await.expect("daemon join"), Ok(()));

    validate_and_publish(&harness, job_id).await;
    assert_published(&harness, job_id, 1).await;
    let pointers: (Option<String>, Option<String>) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("publication pointers");
    assert_eq!(pointers, (Some(job_id.to_string()), None));

    let usage: (i64, String, String, Option<i64>, Option<i64>, Option<i64>) = sqlx::query_as(
        "SELECT count(*),min(state),min(tool),min(input_tokens),min(output_tokens),min(credits_micros) FROM ai_usage WHERE job_id=?",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("Codex usage");
    assert_eq!(
        usage,
        (
            1,
            "final".into(),
            "codex_cli".into(),
            Some(41),
            Some(17),
            None
        )
    );

    let note_artifacts: BTreeMap<String, String> = sqlx::query_as(
        "SELECT a.name,a.relative_path FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? AND t.task_key='note'",
    )
    .bind(job_id.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("note artifacts")
    .into_iter()
    .collect();
    let artifact = |name: &str| {
        fs::read(harness.root.join("artifacts").join(&note_artifacts[name])).expect("NAS artifact")
    };
    assert_eq!(artifact("smart_note"), b"AI note");
    assert_eq!(artifact("summary"), b"AI summary");
    assert_eq!(
        artifact("terms"),
        br#"{"schema":"flori.terms.v1","terms":[]}"#
    );
    let audit: AiAudit = serde_json::from_slice(&artifact("audit")).expect("strict AI audit");
    assert_eq!(
        (audit.tool, audit.model.as_str()),
        (AiTool::CodexCli, "model-a")
    );
    assert_eq!(audit.effort, "high");
    assert_eq!(audit.usage_invocation_keys, ["primary"]);
    assert_eq!((audit.exit_code, audit.timed_out), (Some(0), false));
    assert!(audit.websearch_enabled);
    assert!(audit.websearch_urls.is_empty());

    let argv = fs::read_to_string(captured_argv).expect("captured argv");
    let stdin = fs::read_to_string(captured_stdin).expect("captured stdin");
    let log = String::from_utf8(artifact("log")).expect("task log");
    assert!(stdin.contains("PROMPT 4\nnote\n"));
    assert!(stdin.contains(r#"{"schema":"flori.document_structure.v1","pages":[]}"#));
    assert!(!argv.contains("flori.document_structure.v1"));
    assert!(!argv.contains(&stdin));
    assert!(log.contains("AI task started"));
    assert!(!log.contains("flori.document_structure.v1"));
    assert!(!log.contains(&stdin));
    assert!(
        [&argv, &stdin, &log]
            .into_iter()
            .all(|surface| !surface.contains(&harness.runner_token))
    );
    assert!(
        audit
            .redacted_arguments
            .iter()
            .all(|argument| !argument.contains("flori.document_structure.v1")
                && !argument.contains(&harness.runner_token))
    );
    harness.close().await;
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

async fn content_get(harness: &Harness, path: &str, token: &str, range: Option<&str>) -> Vec<u8> {
    let range = range.map_or(String::new(), |value| format!("Range: {value}\r\n"));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\
         X-Flori-Protocol: 1\r\nAuthorization: Bearer {token}\r\n\
         Content-Length: 0\r\n{range}\r\n"
    );
    let mut stream = TcpStream::connect(harness.address).await.expect("connect");
    stream.write_all(request.as_bytes()).await.expect("write");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("read");
    response
}

fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response)
        .expect("HTTP")
        .split_whitespace()
        .nth(1)
        .expect("status")
        .parse()
        .expect("numeric status")
}

fn header<'a>(response: &'a [u8], name: &str) -> Option<&'a str> {
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")?;
    std::str::from_utf8(&response[..split])
        .ok()?
        .split("\r\n")
        .skip(1)
        .find_map(|line| {
            let (key, value) = line.split_once(':')?;
            key.eq_ignore_ascii_case(name).then(|| value.trim())
        })
}

fn body(response: &[u8]) -> &[u8] {
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("header terminator");
    &response[split + 4..]
}

fn assert_error(response: &[u8], expected_status: u16, expected_code: ErrorCode) {
    assert_eq!(status(response), expected_status);
    let error: flori_core::ErrorResponse = serde_json::from_slice(body(response)).expect("error");
    assert_eq!(error.error.code, expected_code);
}
