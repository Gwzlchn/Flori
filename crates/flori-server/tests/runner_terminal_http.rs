use std::{fs, path::PathBuf, sync::Arc};

use flori_core::{
    AiModelCapability, AiTool, CreateRunnerSlot, DomainId, ErrorCode, JobInputs, JobTrigger,
    LogFrame, PipelineId, PipelineRevisionId, PromptSnapshot, PromptSnapshotId,
    PromptSnapshotProfile, PromptSnapshotPrompt, RegisterRunnerRequest, RunnerTool,
    RunnerToolCapability, Sha256Digest, UsageUpdate,
};
use flori_pipeline::compile;
use flori_runner::RunnerClient;
use flori_store::{CreateJob, CreateSource, Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{net::TcpListener, task::JoinHandle};

struct Harness {
    root: PathBuf,
    client: RunnerClient,
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
        drop(pool);
        Self {
            root,
            client: RunnerClient::new(&base, registered.token).expect("runner client"),
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

#[tokio::test]
async fn runner_client_reaches_foundation_routes() {
    let harness = Harness::new(60_000).await;
    let claim = harness.client.poll().await.expect("poll").expect("claim");
    harness.client.renew(claim.exec_id).await.expect("renew");
    let line = r#"{"message":"ok"}"#.to_owned();
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
