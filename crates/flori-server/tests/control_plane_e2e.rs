use std::{
    collections::BTreeMap, fs, os::unix::fs::PermissionsExt, path::PathBuf, sync::Arc,
    time::Duration,
};

use flori_core::{
    AiAudit, AiModelCapability, AiRunnerSelection, AiTool, ArtifactId, ArtifactKind,
    ArtifactManifestEntry, CreateRunnerSlot, CreatedJob, DocumentStructure, DomainId, ErrorCode,
    EvidenceId, EvidenceManifest, Executor, FailAttemptRequest, JobId, JobInputs, JobTrigger,
    LogFrame, PipelineId, PipelineRevisionId, PromptSnapshot, PromptSnapshotId,
    PromptSnapshotProfile, PromptSnapshotPrompt, RegisterRunnerRequest, RerunJobRequest, RerunMode,
    ResolvedTaskInputs, RunnerId, RunnerTool, RunnerToolCapability, Sha256Digest, SourceInputId,
    SourceKind, StartUploadRequest, TaskClaim, TaskLogLevel, TaskLogLine, UsageOrigin, UsageUpdate,
    VerifyUploadRequest,
};
use flori_pipeline::compile;
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
    pool: SqlitePool,
    client: RunnerClient,
    source_id: flori_core::SourceId,
    address: std::net::SocketAddr,
    runner_token: String,
    other_runner_token: String,
    other_runner_id: RunnerId,
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
        sqlx::query("INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('document_translate','translate',?,0)")
            .bind(digest(b"translate").as_str()).execute(&pool).await.expect("translate prompt");
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
                collection_ids: &[],
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
                collection_ids: &[],
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
        let other_runner = RunnerClient::register(&base, "other-registration", &capabilities())
            .await
            .expect("register other runner");
        let other_runner_token = other_runner.token;
        (
            Self {
                root,
                pool,
                client,
                source_id,
                address,
                runner_token,
                other_runner_token,
                other_runner_id: other_runner.runner_id,
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

fn artifact_body(
    claim: &TaskClaim,
    kind: ArtifactKind,
    evidence: Option<(EvidenceId, ArtifactId)>,
) -> (&'static str, Vec<u8>) {
    match kind {
        ArtifactKind::SourceOriginal => ("application/pdf", b"%PDF-1.7\n".to_vec()),
        ArtifactKind::DocumentStructure => (
            "application/json",
            format!(
                r#"{{"schema":"flori.document_structure.v1","source_artifact_id":"{}","language":"en","pages":[{{"page":1,"width_pt":100.0,"height_pt":200.0}}],"sections":[{{"id":"section-1","heading":"Introduction","blocks":[{{"page":1,"bbox":{{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0}},"text":"Attention is all you need."}}]}}],"figures":[],"tables":[]}}"#,
                match &claim.resolved_inputs {
                    ResolvedTaskInputs::DocumentExtract { pdf } => pdf.artifact_id,
                    _ => panic!("document structure requires extract inputs"),
                }
            )
            .into_bytes(),
        ),
        ArtifactKind::SmartNote => {
            let (id, _) = evidence.expect("note evidence");
            (
                "text/markdown",
                format!("# Smart note\n\n## 来源事实\nAttention is all you need. [[evidence:{id}]]\n\n## AI 分析\nThe source motivates attention.\n").into_bytes(),
            )
        }
        ArtifactKind::Summary => {
            let (id, _) = evidence.expect("summary evidence");
            (
                "text/markdown",
                format!("Attention is all you need. [[evidence:{id}]]\n").into_bytes(),
            )
        }
        ArtifactKind::Terms => (
            "application/json",
            {
                let (id, source) = evidence.expect("terms evidence");
                format!(
                    r#"{{"schema":"flori.terms.v1","terms":[{{"term":"Attention","explanation":"A mechanism that relates positions.","evidence_ids":["{id}"]}}],"evidence_candidates":[{{"evidence_id":"{id}","source_artifact_id":"{source}","locator":{{"kind":"pdf","value":{{"page":1,"bbox":{{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0}}}}}},"quote":"Attention is all you need."}}]}}"#,
                )
                .into_bytes()
            },
        ),
        ArtifactKind::AiAudit => (
            "application/json",
            br#"{"tool":"codex_cli","status":"ok"}"#.to_vec(),
        ),
        ArtifactKind::Translation => (
            "text/markdown",
            b"# Translation\n\nAttention is all you need.\n".to_vec(),
        ),
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
    if matches!(
        claim.executor,
        Executor::AiDocumentNote | Executor::AiDocumentTranslate
    ) {
        assert_eq!(
            (claim.model.as_deref(), claim.effort.as_deref()),
            (Some("model-a"), Some("high"))
        );
        let invocation_key = match claim.executor {
            Executor::AiDocumentNote => "note-call",
            Executor::AiDocumentTranslate => "translate-call",
            _ => unreachable!("matched AI document executor"),
        };
        let started = client
            .update_usage(
                claim.exec_id,
                &UsageUpdate::Started {
                    invocation_key: invocation_key.into(),
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
                    invocation_key: invocation_key.into(),
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
    let evidence = note_evidence(client, claim).await;
    let mut entries = Vec::new();
    for output in claim
        .output_declarations
        .iter()
        .filter(|output| output.required && output.kind != ArtifactKind::TaskLog)
    {
        let (media, bytes) = artifact_body(claim, output.kind, evidence);
        entries.push(upload(client, claim, &output.name, media, &bytes).await);
    }
    let manifest =
        manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, entries).expect("manifest");
    client
        .complete(claim.exec_id, manifest)
        .await
        .expect("complete");
}

async fn fail_runner_task(client: &RunnerClient, claim: &TaskClaim, error_code: ErrorCode) {
    let line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: 1,
        level: TaskLogLevel::Error,
        message: format!("{} failed", claim.task_key),
    })
    .expect("failure log line");
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
        .expect("append failure log");
    let declaration = claim
        .output_declarations
        .iter()
        .find(|output| output.kind == ArtifactKind::AiAudit)
        .expect("AI audit declaration");
    let (media, bytes) = artifact_body(claim, ArtifactKind::AiAudit, None);
    let entry = upload(client, claim, &declaration.name, media, &bytes).await;
    let manifest = manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, vec![entry])
        .expect("failure manifest");
    client
        .fail(
            claim.exec_id,
            &FailAttemptRequest {
                error_code,
                manifest_sha256: Some(manifest),
            },
        )
        .await
        .expect("fail Task");
}

async fn note_evidence(
    client: &RunnerClient,
    claim: &TaskClaim,
) -> Option<(EvidenceId, ArtifactId)> {
    let ResolvedTaskInputs::AiDocumentNote { document, .. } = &claim.resolved_inputs else {
        return None;
    };
    let path = std::env::temp_dir().join(format!("flori-document-{}", claim.exec_id));
    client
        .download_artifact(document, &path)
        .await
        .expect("download document structure");
    let bytes = fs::read(&path).expect("read document structure");
    fs::remove_file(path).expect("remove downloaded structure");
    let document: DocumentStructure =
        serde_json::from_slice(&bytes).expect("strict document structure");
    Some((EvidenceId::generate(), document.source_artifact_id))
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
    assert!(harness.client.poll().await.expect("drive core").is_none());
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
            "note/terms:terms",
            "validate/evidence:evidence"
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
    assert_eq!(artifacts.len(), 10);
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

async fn assert_pdf_artifact_ids(harness: &Harness, job_id: JobId) {
    let original: ArtifactId = sqlx::query_scalar::<_, String>(
        "SELECT id FROM artifacts WHERE job_id=? AND kind='source_original'",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("source original")
    .parse()
    .expect("typed source original");
    let structure_path: String = sqlx::query_scalar(
        "SELECT relative_path FROM artifacts WHERE job_id=? AND kind='document_structure'",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("document structure path");
    let structure: DocumentStructure = serde_json::from_slice(
        &fs::read(harness.root.join("artifacts").join(structure_path)).expect("document bytes"),
    )
    .expect("strict document structure");
    assert_eq!(structure.source_artifact_id, original);
    let evidence_path: String = sqlx::query_scalar(
        "SELECT relative_path FROM artifacts WHERE job_id=? AND kind='evidence'",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("evidence path");
    let evidence: EvidenceManifest = serde_json::from_slice(
        &fs::read(harness.root.join("artifacts").join(evidence_path)).expect("evidence bytes"),
    )
    .expect("strict evidence");
    assert!(
        !evidence.items.is_empty()
            && evidence
                .items
                .iter()
                .all(|item| item.source_artifact_id == original)
    );
}

#[path = "control_plane_e2e/codex.rs"]
mod codex;
#[path = "control_plane_e2e/pdf.rs"]
mod pdf;

async fn rerun_http(harness: &Harness, base_job_id: JobId, request: &RerunJobRequest) -> JobId {
    let response = post_json(
        harness,
        &format!("/api/v1/jobs/{base_job_id}/rerun"),
        &serde_json::to_string(request).expect("rerun request JSON"),
    )
    .await;
    assert_eq!(status(&response), 200);
    serde_json::from_slice::<CreatedJob>(body(&response))
        .expect("created Job response")
        .job_id
}

async fn post_json(harness: &Harness, path: &str, json: &str) -> Vec<u8> {
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\
         X-Flori-Protocol: 1\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\n\r\n{json}",
        json.len()
    );
    let mut stream = TcpStream::connect(harness.address).await.expect("connect");
    stream.write_all(request.as_bytes()).await.expect("write");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("read");
    response
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
