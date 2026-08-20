use std::{fmt::Write, fs, path::PathBuf, sync::Arc};

use flori_core::{
    AiModelCapability, AiTool, AiUsageState, AttemptId, CompiledTaskSpec, CreateRunnerSlot,
    CredentialId, DomainId, ErrorCode, Executor, JobId, JobTrigger, LogFrame, PipelineId,
    PipelineRevisionId, PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile,
    PromptSnapshotPrompt, RegisterRunnerRequest, ResolvedTaskInputs, RunnerId, RunnerTool,
    RunnerToolCapability, Sha256Digest, TaskId, TaskInputBindings, TaskInputReference, UsageOrigin,
    UsageUpdate,
};
use flori_pipeline::compile;
use flori_store::{CreateJob, CreateSource, Store};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

struct TestDatabase {
    directory: PathBuf,
    path: PathBuf,
}

impl TestDatabase {
    fn new() -> Self {
        let directory =
            std::env::temp_dir().join(format!("flori-runner-execution-{}", JobId::generate()));
        fs::create_dir(&directory).expect("create test directory");
        let path = directory.join("flori.db");
        Self { directory, path }
    }

    async fn pool(&self) -> SqlitePool {
        SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&self.path)
                .foreign_keys(true),
        )
        .await
        .expect("connect test database")
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove test directory");
    }
}

struct Foundation {
    store: Store,
    pool: SqlitePool,
    runner_id: RunnerId,
    job_id: JobId,
    task_id: TaskId,
    credential_value: String,
}

async fn foundation(database: &TestDatabase, runner_tags: &[&str]) -> Foundation {
    let store = Store::open(&database.path).await.expect("store");
    let pool = database.pool().await;
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
    let compilation =
        compile("pdf", include_bytes!("../../../pipelines/pdf.yml")).expect("compile PDF pipeline");
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    store
        .register_pipeline_revision(
            pipeline_id,
            revision_id,
            &compilation,
            "test",
            include_str!("../../../pipelines/pdf.yml"),
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
    let credential_id = CredentialId::generate();
    let credential_value = "TEST_COOKIE_\"LINE\nVALUE".to_owned();
    sqlx::query(
        "INSERT INTO credentials(id,kind,name,plaintext_value,created_at_ms,updated_at_ms) \
         VALUES(?,'bilibili_cookie',?,?,2,2)",
    )
    .bind(credential_id.to_string())
    .bind(format!("credential-{credential_id}"))
    .bind(&credential_value)
    .execute(&pool)
    .await
    .expect("credential");
    sqlx::query("UPDATE sources SET credential_id=? WHERE id=?")
        .bind(credential_id.to_string())
        .bind(source_id.to_string())
        .execute(&pool)
        .await
        .expect("attach credential");
    let prompt = "write note";
    let snapshot = PromptSnapshot {
        profile: PromptSnapshotProfile {
            domain_id,
            profile_text: "profile".into(),
            sha256: digest("profile"),
        },
        prompts: vec![PromptSnapshotPrompt {
            key: "document_note".into(),
            content: prompt.into(),
            sha256: digest(prompt),
        }],
    };
    let job_id = store
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
                translate: false,
                created_at_ms: 3,
            },
            &compilation,
        )
        .await
        .expect("job");
    let task_id: String =
        sqlx::query_scalar("SELECT id FROM tasks WHERE job_id=? AND task_key='acquire'")
            .bind(job_id.to_string())
            .fetch_one(&pool)
            .await
            .expect("acquire task");
    let registration = digest("registration-token");
    let runner_id = store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: "runner".into(),
                tags: runner_tags.iter().map(|tag| (*tag).into()).collect(),
                max_concurrency: 1,
                default_model: Some("gpt-5.6".into()),
                default_effort: Some("high".into()),
            },
            &registration,
            100,
            4,
        )
        .await
        .expect("runner slot");
    store
        .register_runner(
            &registration,
            &digest("long-token"),
            &RegisterRunnerRequest {
                tools: vec![
                    RunnerToolCapability {
                        tool: RunnerTool::PdfExtractor,
                        version: "1.0".into(),
                    },
                    RunnerToolCapability {
                        tool: RunnerTool::QoderCli,
                        version: "1.0".into(),
                    },
                ],
                ai_models: vec![AiModelCapability {
                    model: "gpt-5.6".into(),
                    efforts: vec!["high".into()],
                }],
            },
            5,
        )
        .await
        .expect("register runner");
    Foundation {
        store,
        pool,
        runner_id,
        job_id,
        task_id: task_id.parse().expect("task ID"),
        credential_value,
    }
}

#[tokio::test]
async fn pipeline_object_must_exactly_match_its_canonical_json() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let yaml = include_bytes!("../../../pipelines/pdf.yml");
    let mut forged = compile("pdf", yaml).expect("compile PDF pipeline");
    forged.pipeline.pipeline_key = "forged-pdf".into();
    assert_eq!(
        store
            .register_pipeline_revision(
                PipelineId::generate(),
                PipelineRevisionId::generate(),
                &forged,
                "test",
                std::str::from_utf8(yaml).expect("UTF-8"),
                1,
            )
            .await
            .expect_err("pipeline object must equal canonical JSON")
            .code(),
        ErrorCode::InvalidRequest
    );
}

#[tokio::test]
async fn concurrent_poll_has_one_winner_and_secret_exists_only_in_claim() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let store = Arc::new(foundation.store);
    let (left, right) = tokio::join!(
        store.poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example"),
        store.poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example"),
    );
    let claims = [left.expect("left poll"), right.expect("right poll")];
    assert_eq!(claims.iter().filter(|claim| claim.is_some()).count(), 1);
    let claim = claims.into_iter().flatten().next().expect("winning claim");
    assert_eq!(claim.job_id, foundation.job_id);
    assert_eq!(claim.task_id, foundation.task_id);
    assert_eq!(claim.attempt_no, 1);
    assert_eq!(
        claim.secret_inputs.credential.expect("cookie").value,
        foundation.credential_value
    );
    assert!(matches!(
        claim.resolved_inputs,
        ResolvedTaskInputs::DocumentAcquire { .. }
    ));
    let persisted: String =
        sqlx::query_scalar("SELECT spec_json || input_bindings_json FROM tasks WHERE id=?")
            .bind(foundation.task_id.to_string())
            .fetch_one(&foundation.pool)
            .await
            .expect("persisted task");
    assert!(!persisted.contains(&foundation.credential_value));
    let attempts: i64 = sqlx::query_scalar("SELECT count(*) FROM attempts WHERE task_id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("attempt count");
    assert_eq!(attempts, 1);
}

#[tokio::test]
async fn poll_waits_on_tag_tool_and_pinned_model_capability_mismatch() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["ai"]).await;
    assert!(
        foundation
            .store
            .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
            .await
            .expect("tag mismatch poll")
            .is_none()
    );
    let remote_http = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "http://flori.example")
        .await;
    let error = match remote_http {
        Err(error) => error,
        Ok(_) => panic!("remote plain HTTP must be rejected"),
    };
    assert_eq!(error.code(), ErrorCode::InvalidRequest);
    let extract_spec = CompiledTaskSpec {
        executor: Executor::DocumentExtract,
        needs: Vec::new(),
        tags: vec!["ai".into()],
        retry: 0,
        timeout_ms: 1_000,
        artifacts: Vec::new(),
    };
    let extract_bindings = TaskInputBindings::DocumentExtract {
        pdf: TaskInputReference::NeedArtifact {
            task: "acquire".into(),
            artifact: "original".into(),
        },
    };
    let qoder_only = vec![RunnerToolCapability {
        tool: RunnerTool::QoderCli,
        version: "1.0".into(),
    }];
    sqlx::query("UPDATE runners SET tools_json=? WHERE id=?")
        .bind(serde_json::to_string(&qoder_only).expect("tools JSON"))
        .bind(foundation.runner_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("remove PDF capability");
    sqlx::query(
        "UPDATE tasks SET executor='document.extract',spec_json=?,input_bindings_json=?, \
         attempt_limit=1,timeout_ms=1000 WHERE id=?",
    )
    .bind(serde_json::to_string(&extract_spec).expect("extract spec JSON"))
    .bind(serde_json::to_string(&extract_bindings).expect("extract bindings JSON"))
    .bind(foundation.task_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("make extract task");
    assert!(
        foundation
            .store
            .poll_and_claim(foundation.runner_id, 10, 70, "http://localhost:8080/")
            .await
            .expect("missing tool poll")
            .is_none()
    );
    let spec = CompiledTaskSpec {
        executor: Executor::AiDocumentNote,
        needs: Vec::new(),
        tags: vec!["ai".into()],
        retry: 0,
        timeout_ms: 1_000,
        artifacts: Vec::new(),
    };
    let bindings = TaskInputBindings::AiDocumentNote {
        document: TaskInputReference::NeedArtifact {
            task: "extract".into(),
            artifact: "structure".into(),
        },
        prompt: TaskInputReference::Prompt("document_note".into()),
        profile: None,
    };
    sqlx::query(
        "UPDATE tasks SET executor='ai.document_note',spec_json=?,input_bindings_json=?, \
         attempt_limit=1,timeout_ms=1000,pinned_runner_id=?,selected_model='missing-model', \
         selected_effort='high',runner_config_revision=1 WHERE id=?",
    )
    .bind(serde_json::to_string(&spec).expect("spec JSON"))
    .bind(serde_json::to_string(&bindings).expect("bindings JSON"))
    .bind(foundation.runner_id.to_string())
    .bind(foundation.task_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("pin AI task");
    assert!(
        foundation
            .store
            .poll_and_claim(foundation.runner_id, 11, 71, "https://flori.example")
            .await
            .expect("capability mismatch poll")
            .is_none()
    );
}

#[tokio::test]
async fn log_sequence_is_idempotent_strict_and_fenced_after_attempt_end() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    let first = frame(1, r#"{"message":"first"}"#);
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                foundation.runner_id,
                claim.exec_id,
                std::slice::from_ref(&first),
                11,
            )
            .await
            .expect("first log")
            .last_sequence,
        1
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(foundation.runner_id, claim.exec_id, &[first], 12)
            .await
            .expect("idempotent log")
            .last_sequence,
        1
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                foundation.runner_id,
                claim.exec_id,
                &[frame(1, r#"{"message":"changed"}"#)],
                13,
            )
            .await
            .expect_err("same sequence conflicts")
            .code(),
        ErrorCode::LogSequenceConflict
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                foundation.runner_id,
                claim.exec_id,
                &[frame(3, r#"{"message":"gap"}"#)],
                14,
            )
            .await
            .expect_err("gap rejected")
            .code(),
        ErrorCode::LogSequenceGap
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                foundation.runner_id,
                claim.exec_id,
                &[frame(2, &json_escaped(&foundation.credential_value))],
                15,
            )
            .await
            .expect_err("credential cannot enter logs")
            .code(),
        ErrorCode::CredentialUnavailable
    );
    foundation
        .store
        .append_log_frames(
            foundation.runner_id,
            claim.exec_id,
            &[frame(2, r#"{"message":"second"}"#)],
            16,
        )
        .await
        .expect("second log");
    foundation
        .store
        .fail_attempt(claim.exec_id, ErrorCode::ExecutorFailed, "failed", 17)
        .await
        .expect("finish attempt");
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                foundation.runner_id,
                claim.exec_id,
                &[frame(3, r#"{"message":"late"}"#)],
                18,
            )
            .await
            .expect_err("late log rejected")
            .code(),
        ErrorCode::StaleAttempt
    );
}

#[tokio::test]
async fn log_limit_counts_the_complete_ndjson_frame() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let spec_json: String = sqlx::query_scalar("SELECT spec_json FROM tasks WHERE id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("task spec");
    let mut spec: CompiledTaskSpec = serde_json::from_str(&spec_json).expect("strict task spec");
    spec.artifacts
        .iter_mut()
        .find(|artifact| artifact.kind == flori_core::ArtifactKind::TaskLog)
        .expect("task log declaration")
        .max_bytes = 1;
    sqlx::query("UPDATE tasks SET spec_json=? WHERE id=?")
        .bind(serde_json::to_string(&spec).expect("updated spec"))
        .bind(foundation.task_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("small log limit");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    assert_eq!(
        foundation
            .store
            .append_log_frames(foundation.runner_id, claim.exec_id, &[frame(1, "")], 11)
            .await
            .expect_err("frame metadata counts against declaration")
            .code(),
        ErrorCode::ArtifactTooLarge
    );
}

#[tokio::test]
async fn usage_bridge_is_idempotent_and_only_existing_usage_can_finish_late() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let exec_id = AttemptId::generate();
    sqlx::query("UPDATE jobs SET state='running',started_at_ms=10 WHERE id=?")
        .bind(foundation.job_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("start job");
    sqlx::query("UPDATE tasks SET executor='ai.document_note',state='leased',selected_model='gpt-5.6',selected_effort='high',runner_config_revision=1,started_at_ms=10 WHERE id=?")
        .bind(foundation.task_id.to_string()).execute(&foundation.pool).await.expect("lease task state");
    sqlx::query("INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,model,effort,runner_config_revision,lease_expires_at_ms,last_log_sequence,started_at_ms) VALUES(?,?,1,?,'leased','gpt-5.6','high',1,70,0,10)")
        .bind(exec_id.to_string()).bind(foundation.task_id.to_string()).bind(foundation.runner_id.to_string())
        .execute(&foundation.pool).await.expect("attempt");
    sqlx::query("UPDATE tasks SET current_attempt_id=? WHERE id=?")
        .bind(exec_id.to_string())
        .bind(foundation.task_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("current attempt");
    let started = UsageUpdate::Started {
        invocation_key: "note-1".into(),
        tool: AiTool::QoderCli,
        model: "gpt-5.6".into(),
        effort: "high".into(),
    };
    let first = foundation
        .store
        .apply_usage_update(foundation.runner_id, exec_id, &started, 11)
        .await
        .expect("start usage");
    let repeated = foundation
        .store
        .apply_usage_update(foundation.runner_id, exec_id, &started, 12)
        .await
        .expect("repeat usage");
    assert_eq!(first, repeated);
    assert_eq!(first.state, AiUsageState::Started);
    sqlx::query("UPDATE attempts SET state='failed',finished_at_ms=13 WHERE id=?")
        .bind(exec_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("fail attempt");
    sqlx::query("UPDATE tasks SET state='failed',finished_at_ms=13 WHERE id=?")
        .bind(foundation.task_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("fail task");
    sqlx::query("UPDATE jobs SET state='failed',finished_at_ms=13 WHERE id=?")
        .bind(foundation.job_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("fail job");
    let final_update = UsageUpdate::Final {
        invocation_key: "note-1".into(),
        origin: UsageOrigin::Observed,
        input_tokens: Some(10),
        output_tokens: Some(20),
        cost_micros: None,
        credits_micros: Some(30),
    };
    let finalized = foundation
        .store
        .apply_usage_update(foundation.runner_id, exec_id, &final_update, 14)
        .await
        .expect("existing usage final may arrive late");
    assert_eq!(finalized.state, AiUsageState::Final);
    assert_eq!(
        foundation
            .store
            .apply_usage_update(
                foundation.runner_id,
                exec_id,
                &UsageUpdate::Started {
                    invocation_key: "late-new".into(),
                    tool: AiTool::QoderCli,
                    model: "gpt-5.6".into(),
                    effort: "high".into(),
                },
                15,
            )
            .await
            .expect_err("new usage cannot start late")
            .code(),
        ErrorCode::StaleAttempt
    );
    assert_eq!(
        foundation
            .store
            .apply_usage_update(RunnerId::generate(), exec_id, &final_update, 16)
            .await
            .expect_err("wrong runner cannot finalize")
            .code(),
        ErrorCode::StaleAttempt
    );
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("write digest");
    }
    Sha256Digest::parse(output).expect("digest")
}

fn frame(sequence: u64, line: &str) -> LogFrame {
    LogFrame {
        sequence,
        sha256: digest(line),
        line: line.into(),
    }
}

fn json_escaped(value: &str) -> String {
    serde_json::to_string(value)
        .expect("JSON string")
        .trim_matches('"')
        .to_owned()
}
