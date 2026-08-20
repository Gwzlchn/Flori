use std::{fs, path::PathBuf, sync::Arc};

use flori_core::{
    AiModelCapability, AiTool, ArtifactManifestEntry, AttemptState, CreateRunnerSlot, DomainId,
    ErrorCode, FailAttemptRequest, JobInputs, JobTrigger, LogFrame, PipelineId, PipelineRevisionId,
    PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile, PromptSnapshotPrompt,
    RegisterRunnerRequest, RunnerTool, RunnerToolCapability, Sha256Digest, StartUploadRequest,
    TaskLogLevel, TaskLogLine, UsageUpdate, VerifyUploadRequest,
};
use flori_pipeline::compile;
use flori_runner::{RunnerClient, manifest_sha256};
use flori_store::{CreateJob, CreateSource, Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{net::TcpListener, task::JoinHandle};

struct Harness {
    root: PathBuf,
    artifact_root: PathBuf,
    client: RunnerClient,
    other: RunnerClient,
    server: Option<JoinHandle<()>>,
}

impl Harness {
    async fn new(lease_ms: u64) -> Self {
        let root = std::env::temp_dir().join(format!(
            "flori-server-terminal-{}",
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
        sqlx::query(
            "INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) \
             VALUES(?,?,?,'profile',0,0)",
        )
        .bind(domain_id.to_string())
        .bind(format!("domain-{domain_id}"))
        .bind("Domain")
        .execute(&pool)
        .await
        .expect("domain");
        sqlx::query(
            "INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('document_note','note',?,0)",
        )
        .bind(digest(b"note").as_str())
        .execute(&pool)
        .await
        .expect("prompt");
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
                kind: flori_core::SourceKind::PdfUrl,
                canonical_ref: "https://example.test/paper.pdf",
                title: None,
                domain_id,
                collection_ids: &[],
                request_key: "source-request",
                request_sha256: &"a".repeat(64),
                created_at_ms: 2,
            })
            .await
            .expect("source");
        let snapshot = PromptSnapshot {
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
        };
        store
            .create_job(
                CreateJob {
                    source_id,
                    pipeline_revision_id: revision_id,
                    trigger: JobTrigger::Initial,
                    rerun_of_job_id: None,
                    prompt_snapshot_id: PromptSnapshotId::generate(),
                    prompt_snapshot: &snapshot,
                    request_key: "job-request",
                    request_sha256: &"b".repeat(64),
                    inputs: JobInputs { translate: false },
                    created_at_ms: 3,
                },
                &compilation,
            )
            .await
            .expect("job");
        create_slot(&store, "runner", "registration-one").await;
        create_slot(&store, "other", "registration-two").await;
        let artifact_root = root.join("artifacts");
        let artifacts = Arc::new(
            NasArtifactStore::new(&artifact_root, 128 * 1024 * 1024).expect("artifact store"),
        );
        let listener = TcpListener::bind("localhost:0").await.expect("bind server");
        let address = listener.local_addr().expect("server address");
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(
                    store,
                    artifacts,
                    "http://localhost/content".into(),
                    lease_ms,
                )
                .expect("server app"),
            )
            .await
            .expect("serve");
        });
        let base = format!("http://{address}");
        let registered = RunnerClient::register(&base, "registration-one", &capabilities())
            .await
            .expect("register runner");
        let other = RunnerClient::register(&base, "registration-two", &capabilities())
            .await
            .expect("register other runner");
        drop(pool);
        Self {
            root,
            artifact_root,
            client: RunnerClient::new(&base, registered.token).expect("runner client"),
            other: RunnerClient::new(&base, other.token).expect("other runner client"),
            server: Some(server),
        }
    }

    async fn close(mut self) {
        let server = self.server.take().expect("server handle");
        server.abort();
        let _ = server.await;
        let root = self.root.clone();
        drop(self);
        fs::remove_dir_all(root).expect("remove fixture");
    }
}

async fn create_slot(store: &Store, name: &str, token: &str) {
    store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: name.into(),
                tags: vec!["media".into()],
                max_concurrency: 1,
                default_model: None,
                default_effort: None,
            },
            &digest(token.as_bytes()),
            i64::MAX,
            1,
        )
        .await
        .expect("runner slot");
}

fn capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![RunnerToolCapability {
            tool: RunnerTool::PdfExtractor,
            version: "1.0".into(),
        }],
        ai_models: Vec::<AiModelCapability>::new(),
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

fn task_log_line(message: &str) -> String {
    serde_json::to_string(&TaskLogLine {
        timestamp_ms: 1,
        level: TaskLogLevel::Info,
        message: message.into(),
    })
    .expect("task log line")
}

fn files_under(root: &std::path::Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in fs::read_dir(root).expect("read artifact directory") {
        let path = entry.expect("artifact entry").path();
        if path.is_dir() {
            files.extend(files_under(&path));
        } else {
            files.push(path);
        }
    }
    files
}

async fn upload(
    client: &RunnerClient,
    exec_id: flori_core::AttemptId,
    name: &str,
    media_type: &str,
    bytes: &[u8],
) -> ArtifactManifestEntry {
    let request = StartUploadRequest {
        name: name.into(),
        media_type: media_type.into(),
        size_bytes: bytes.len() as u64,
        sha256: digest(bytes),
    };
    let started = client
        .start_upload(exec_id, &request)
        .await
        .expect("start upload");
    client
        .append_upload_chunk(started.upload_id, started.received_bytes, bytes.to_vec())
        .await
        .expect("append upload");
    client
        .verify_upload(
            started.upload_id,
            &VerifyUploadRequest {
                size_bytes: request.size_bytes,
                sha256: request.sha256,
            },
        )
        .await
        .expect("verify upload")
        .artifact
}

#[tokio::test]
async fn runner_client_reaches_foundation_routes() {
    let harness = Harness::new(60_000).await;
    let claim = harness.client.poll().await.expect("poll").expect("claim");
    harness.client.renew(claim.exec_id).await.expect("renew");
    let line = task_log_line("ok");
    harness
        .client
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
    let usage = harness
        .client
        .update_usage(
            claim.exec_id,
            &UsageUpdate::Started {
                invocation_key: "one".into(),
                tool: AiTool::CodexCli,
                model: "model".into(),
                effort: "high".into(),
            },
        )
        .await
        .expect_err("non-AI task rejects usage");
    assert_eq!(usage.code(), ErrorCode::UsageConflict);
    harness.close().await;
}

#[tokio::test]
async fn upload_resumes_and_complete_rejects_corrupt_artifact() {
    let harness = Harness::new(60_000).await;
    let claim = harness.client.poll().await.expect("poll").expect("claim");
    let request = StartUploadRequest {
        name: "original".into(),
        media_type: "application/pdf".into(),
        size_bytes: 3,
        sha256: digest(b"PDF"),
    };
    let started = harness
        .client
        .start_upload(claim.exec_id, &request)
        .await
        .expect("start upload");
    let wrong_runner = harness
        .other
        .start_upload(claim.exec_id, &request)
        .await
        .expect_err("other runner cannot upload");
    assert_eq!(wrong_runner.code(), ErrorCode::StaleAttempt);
    harness
        .client
        .append_upload_chunk(started.upload_id, 0, b"P".to_vec())
        .await
        .expect("first chunk");
    let oversized = harness
        .client
        .append_upload_chunk(started.upload_id, 1, vec![0; 8 * 1024 * 1024 + 1])
        .await
        .expect_err("oversized chunk rejected before storage");
    assert_eq!(oversized.code(), ErrorCode::ArtifactTooLarge);
    let resumed = harness
        .client
        .start_upload(claim.exec_id, &request)
        .await
        .expect("resume upload");
    assert_eq!(resumed.upload_id, started.upload_id);
    assert_eq!(resumed.received_bytes, 1);
    harness
        .client
        .append_upload_chunk(started.upload_id, 1, b"DF".to_vec())
        .await
        .expect("second chunk");
    let original = harness
        .client
        .verify_upload(
            started.upload_id,
            &VerifyUploadRequest {
                size_bytes: 3,
                sha256: digest(b"PDF"),
            },
        )
        .await
        .expect("verify original")
        .artifact;
    let line = task_log_line("complete");
    harness
        .client
        .append_logs(
            claim.exec_id,
            &[LogFrame {
                sequence: 1,
                sha256: digest(line.as_bytes()),
                line,
            }],
        )
        .await
        .expect("append task log");
    let manifest = manifest_sha256(
        claim.job_id,
        claim.task_id,
        claim.exec_id,
        vec![original.clone()],
    )
    .expect("manifest digest");
    let wrong_manifest = harness
        .client
        .complete(claim.exec_id, digest(b"wrong"))
        .await
        .expect_err("wrong manifest rejected");
    assert_eq!(wrong_manifest.code(), ErrorCode::DigestMismatch);
    let artifact_path = harness.artifact_root.join(&original.relative_path);
    fs::write(&artifact_path, b"BAD").expect("mutate artifact");
    let corrupt = harness
        .client
        .complete(claim.exec_id, manifest.clone())
        .await
        .expect_err("mutated artifact rejected");
    assert_eq!(corrupt.code(), ErrorCode::CorruptState);
    fs::write(&artifact_path, b"PDF").expect("restore artifact");
    let completed = harness
        .client
        .complete(claim.exec_id, manifest.clone())
        .await
        .expect("complete attempt");
    assert_eq!(completed.state, AttemptState::Succeeded);
    let repeated = harness
        .client
        .complete(claim.exec_id, manifest)
        .await
        .expect("idempotent complete");
    assert_eq!(repeated.state, AttemptState::Succeeded);
    harness.close().await;
}

#[tokio::test]
async fn fail_commits_only_always_artifacts() {
    let harness = Harness::new(60_000).await;
    let claim = harness.client.poll().await.expect("poll").expect("claim");
    let original = upload(
        &harness.client,
        claim.exec_id,
        "original",
        "application/pdf",
        b"PDF",
    )
    .await;
    let line = task_log_line("failed");
    let expected_log = format!("{line}\n").into_bytes();
    harness
        .client
        .append_logs(
            claim.exec_id,
            &[LogFrame {
                sequence: 1,
                sha256: digest(line.as_bytes()),
                line,
            }],
        )
        .await
        .expect("append task log");
    let failed = harness
        .client
        .fail(
            claim.exec_id,
            &FailAttemptRequest {
                error_code: ErrorCode::ExecutorFailed,
                manifest_sha256: None,
            },
        )
        .await
        .expect("fail attempt");
    assert_eq!(failed.state, AttemptState::Failed);
    assert!(!harness.artifact_root.join(original.relative_path).exists());
    assert!(
        files_under(&harness.artifact_root)
            .into_iter()
            .any(|path| fs::read(path).expect("read artifact") == expected_log)
    );
    harness.close().await;
}

#[tokio::test]
async fn expired_lease_is_rejected() {
    let harness = Harness::new(1).await;
    let claim = harness.client.poll().await.expect("poll").expect("claim");
    tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    let expired = harness
        .client
        .start_upload(
            claim.exec_id,
            &StartUploadRequest {
                name: "original".into(),
                media_type: "application/pdf".into(),
                size_bytes: 3,
                sha256: digest(b"PDF"),
            },
        )
        .await
        .expect_err("expired lease rejects upload");
    assert_eq!(expired.code(), ErrorCode::LeaseExpired);
    harness.close().await;
}
