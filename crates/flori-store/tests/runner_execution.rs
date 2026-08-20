use std::{fmt::Write, fs, path::PathBuf, sync::Arc};

use flori_core::{
    AiModelCapability, AiTool, AiUsageId, AiUsageState, ArtifactDeclaration, ArtifactKind,
    ArtifactManifest, ArtifactWhen, AttemptId, AttemptState, CompiledTaskSpec,
    CompleteAttemptRequest, CreateRunnerSlot, CredentialId, DomainId, ErrorCode, Executor,
    FailAttemptRequest, JobId, JobInputs, JobTrigger, LogFrame, PendingAttemptUpload, PipelineId,
    PipelineRevisionId, PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile,
    PromptSnapshotPrompt, RegisterRunnerRequest, ResolvedTaskInputs, RunnerId, RunnerTool,
    RunnerToolCapability, Sha256Digest, StartUploadRequest, StartUploadResponse, TaskId,
    TaskInputBindings, TaskInputReference, TaskLogEvent, TaskLogLevel, TaskLogLine, UploadState,
    UsageOrigin, UsageUpdate, VerifyUploadRequest,
};
use flori_pipeline::compile;
use flori_store::{
    CreateJob, CreateSource, Store,
    artifact::{NasArtifactStore, RecoveryAction, UploadRecord},
};
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
    source_id: flori_core::SourceId,
    job_id: JobId,
    task_id: TaskId,
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
            collection_ids: &[],
            request_key: "source-request",
            request_sha256: &"a".repeat(64),
            created_at_ms: 2,
        })
        .await
        .expect("source");
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
                inputs: JobInputs { translate: false },
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
                    RunnerToolCapability {
                        tool: RunnerTool::YtDlp,
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
        source_id,
        job_id,
        task_id: task_id.parse().expect("task ID"),
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
    let credential_value = "TEST_COOKIE_VALUE";
    attach_credential(
        &foundation.pool,
        foundation.source_id,
        "youtube_cookie",
        credential_value,
    )
    .await;
    sqlx::query("UPDATE sources SET kind='youtube_video' WHERE id=?")
        .bind(foundation.source_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("video source kind");
    let spec_json: String = sqlx::query_scalar("SELECT spec_json FROM tasks WHERE id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("task spec");
    let mut spec: CompiledTaskSpec = serde_json::from_str(&spec_json).expect("strict spec");
    spec.executor = Executor::VideoAcquire;
    let bindings = TaskInputBindings::VideoAcquire {
        source: TaskInputReference::Source,
    };
    sqlx::query(
        "UPDATE tasks SET executor='video.acquire',spec_json=?,input_bindings_json=? WHERE id=?",
    )
    .bind(serde_json::to_string(&spec).expect("video spec"))
    .bind(serde_json::to_string(&bindings).expect("video bindings"))
    .bind(foundation.task_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("video acquire task");
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
        credential_value
    );
    assert!(matches!(
        claim.resolved_inputs,
        ResolvedTaskInputs::VideoAcquire { .. }
    ));
    let persisted: String =
        sqlx::query_scalar("SELECT spec_json || input_bindings_json FROM tasks WHERE id=?")
            .bind(foundation.task_id.to_string())
            .fetch_one(&foundation.pool)
            .await
            .expect("persisted task");
    assert!(!persisted.contains(credential_value));
    let attempts: i64 = sqlx::query_scalar("SELECT count(*) FROM attempts WHERE task_id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("attempt count");
    assert_eq!(attempts, 1);
}

#[tokio::test]
async fn claim_rejects_platform_cookie_attached_to_the_wrong_source_kind() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    attach_credential(
        &foundation.pool,
        foundation.source_id,
        "bilibili_cookie",
        "WRONG_SOURCE_COOKIE",
    )
    .await;
    let result = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await;
    let error = match result {
        Err(error) => error,
        Ok(_) => panic!("PDF source must not receive a platform cookie"),
    };
    assert_eq!(error.code(), ErrorCode::CredentialUnavailable);
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
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    let credential_value = "TEST_COOKIE_\"LINE\nVALUE";
    attach_credential(
        &foundation.pool,
        foundation.source_id,
        "bilibili_cookie",
        credential_value,
    )
    .await;
    let first = frame(1, "first");
    let (upload_id, final_path, max_bytes, rolling_sha): (String, String, i64, String) =
        sqlx::query_as(
            "SELECT id,final_relative_path,expected_size_bytes,expected_sha256 FROM uploads \
             WHERE owner_kind='attempt' AND owner_id=? AND name='log'",
        )
        .bind(claim.exec_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("server log ledger");
    let mut file_ahead = UploadRecord::new(
        upload_id.parse().expect("upload ID"),
        "log",
        final_path,
        u64::try_from(max_bytes).expect("log max"),
        Sha256Digest::parse(rolling_sha).expect("rolling SHA"),
        "log",
        u64::try_from(max_bytes).expect("log max"),
    )
    .expect("server log record");
    file_ahead
        .restore_progress(0, UploadState::Receiving)
        .expect("empty server log");
    let first_chunk = format!("{}\n", first.line);
    artifacts
        .append_chunk(
            &file_ahead,
            0,
            &digest(&first_chunk),
            first_chunk.as_bytes(),
        )
        .expect("simulate fs-ahead log frame");
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
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
    let payload: String = sqlx::query_scalar(
        "SELECT payload_json FROM job_events WHERE kind='log_cursor' ORDER BY id DESC LIMIT 1",
    )
    .fetch_one(&foundation.pool)
    .await
    .expect("cursor event");
    let event: TaskLogEvent = serde_json::from_str(&payload).expect("strict cursor payload");
    assert_eq!(
        event,
        TaskLogEvent {
            job_id: claim.job_id,
            task_id: claim.task_id,
            attempt_id: claim.exec_id,
            last_sequence: 1,
        }
    );
    assert!(!payload.contains("message"));
    assert!(!payload.contains("sha256"));
    let legacy = r#"{"message":"legacy"}"#.to_owned();
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[LogFrame {
                    sequence: 2,
                    sha256: digest(&legacy),
                    line: legacy,
                }],
                12,
            )
            .await
            .expect_err("legacy message object is not a TaskLogLine")
            .code(),
        ErrorCode::InvalidRequest
    );
    let second = frame(2, "second");
    let (final_path, max_bytes, rolling_sha, received): (String, i64, String, i64) =
        sqlx::query_as(
            "SELECT final_relative_path,expected_size_bytes,expected_sha256,received_bytes \
             FROM uploads WHERE id=?",
        )
        .bind(&upload_id)
        .fetch_one(&foundation.pool)
        .await
        .expect("advanced server log ledger");
    let mut second_ahead = UploadRecord::new(
        upload_id.parse().expect("upload ID"),
        "log",
        final_path,
        u64::try_from(max_bytes).expect("log max"),
        Sha256Digest::parse(rolling_sha).expect("rolling SHA"),
        "log",
        u64::try_from(max_bytes).expect("log max"),
    )
    .expect("advanced server log record");
    second_ahead
        .restore_progress(
            u64::try_from(received).expect("received log bytes"),
            UploadState::Receiving,
        )
        .expect("advanced server log");
    let second_chunk = format!("{}\n", second.line);
    artifacts
        .append_chunk(
            &second_ahead,
            u64::try_from(received).expect("received log bytes"),
            &digest(&second_chunk),
            second_chunk.as_bytes(),
        )
        .expect("simulate second fs-ahead frame");
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[first.clone(), second],
                12,
            )
            .await
            .expect("duplicate prefix reaches fs-ahead frame")
            .last_sequence,
        2
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[first],
                12
            )
            .await
            .expect("idempotent log")
            .last_sequence,
        2
    );
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[frame(1, "changed")],
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
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[frame(4, "gap")],
                14,
            )
            .await
            .expect_err("gap rejected")
            .code(),
        ErrorCode::LogSequenceGap
    );
    let canonical_credential_line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: 1,
        level: TaskLogLevel::Info,
        message: credential_value.into(),
    })
    .expect("canonical secret line");
    let escaped_credential_line =
        canonical_credential_line.replacen(r#""message":"T"#, r#""message":"\u0054"#, 1);
    assert_eq!(
        serde_json::from_str::<TaskLogLine>(&escaped_credential_line)
            .expect("valid escaped line")
            .message,
        credential_value
    );
    let staging = database
        .directory
        .join("artifacts/.staging/uploads")
        .join(&upload_id);
    let before_bytes = fs::read(&staging).expect("staging before rejected secret");
    let before_events: i64 =
        sqlx::query_scalar("SELECT count(*) FROM job_events WHERE kind='log_cursor'")
            .fetch_one(&foundation.pool)
            .await
            .expect("events before rejected secret");
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[LogFrame {
                    sequence: 3,
                    sha256: digest(&escaped_credential_line),
                    line: escaped_credential_line,
                }],
                15,
            )
            .await
            .expect_err("noncanonical Unicode escape is rejected before persistence")
            .code(),
        ErrorCode::InvalidRequest
    );
    assert_eq!(
        fs::read(&staging).expect("staging after rejected secret"),
        before_bytes
    );
    let after_events: i64 =
        sqlx::query_scalar("SELECT count(*) FROM job_events WHERE kind='log_cursor'")
            .fetch_one(&foundation.pool)
            .await
            .expect("events after rejected secret");
    assert_eq!(after_events, before_events);
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[frame(3, credential_value)],
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
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(3, "third")],
            16,
        )
        .await
        .expect("second log");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "log".into(),
                        media_type: "application/x-ndjson".into(),
                        size_bytes: 3,
                        sha256: digest("log"),
                    },
                    17,
                )
                .await,
        ),
        ErrorCode::Conflict
    );
    foundation
        .store
        .fail_authenticated_attempt(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &FailAttemptRequest {
                error_code: ErrorCode::ExecutorFailed,
                manifest_sha256: None,
            },
            20,
        )
        .await
        .expect("finish attempt");
    assert_eq!(
        foundation
            .store
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[frame(4, "late")],
                21,
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
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
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
            .append_log_frames(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &[frame(1, "")],
                11
            )
            .await
            .expect_err("frame metadata counts against declaration")
            .code(),
        ErrorCode::ArtifactTooLarge
    );
}

#[tokio::test]
async fn server_owned_log_supports_log_only_success_and_empty_failure() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let spec_json: String = sqlx::query_scalar("SELECT spec_json FROM tasks WHERE id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("task spec");
    let mut spec: CompiledTaskSpec = serde_json::from_str(&spec_json).expect("task spec");
    spec.artifacts
        .retain(|artifact| artifact.kind == ArtifactKind::TaskLog);
    sqlx::query("UPDATE tasks SET spec_json=? WHERE id=?")
        .bind(serde_json::to_string(&spec).expect("log-only spec"))
        .bind(foundation.task_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("log-only task");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    foundation
        .store
        .append_log_frames(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(1, "only output")],
            11,
        )
        .await
        .expect("task log");
    let request = CompleteAttemptRequest {
        manifest_sha256: manifest_digest(&foundation, claim.exec_id, Vec::new()),
    };
    assert_eq!(
        foundation
            .store
            .complete_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &request,
                12,
            )
            .await
            .expect("log-only completion")
            .state,
        AttemptState::Succeeded
    );
    let names: Vec<String> =
        sqlx::query_scalar("SELECT name FROM artifacts WHERE attempt_id=? ORDER BY name")
            .bind(claim.exec_id.to_string())
            .fetch_all(&foundation.pool)
            .await
            .expect("log artifact");
    assert_eq!(names, vec!["log"]);

    let second_database = TestDatabase::new();
    let second = self::foundation(&second_database, &["media"]).await;
    let second_artifacts = NasArtifactStore::new(second_database.directory.join("artifacts"), 1024)
        .expect("artifact store");
    let empty = second
        .store
        .poll_and_claim(second.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    assert_eq!(
        second
            .store
            .fail_authenticated_attempt(
                &second_artifacts,
                second.runner_id,
                empty.exec_id,
                &FailAttemptRequest {
                    error_code: ErrorCode::ExecutorFailed,
                    manifest_sha256: None,
                },
                11,
            )
            .await
            .expect("empty-log failure")
            .state,
        AttemptState::Failed
    );
    let upload_count: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads WHERE owner_id=?")
        .bind(empty.exec_id.to_string())
        .fetch_one(&second.pool)
        .await
        .expect("cleaned empty log ledger");
    assert_eq!(upload_count, 0);
}

#[tokio::test]
async fn attempt_upload_recovers_cursor_and_rename_crash_windows() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "undeclared".into(),
                        media_type: "application/octet-stream".into(),
                        size_bytes: 3,
                        sha256: digest("abc"),
                    },
                    11,
                )
                .await,
        ),
        ErrorCode::ArtifactUndeclared
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "original".into(),
                        media_type: "text/html".into(),
                        size_bytes: 3,
                        sha256: digest("abc"),
                    },
                    11,
                )
                .await,
        ),
        ErrorCode::InvalidRequest
    );
    assert!(!database.directory.join("artifacts/.staging").exists());
    let request = StartUploadRequest {
        name: "original".into(),
        media_type: "application/pdf".into(),
        size_bytes: 3,
        sha256: digest("abc"),
    };
    let upload = foundation
        .store
        .start_attempt_upload(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &request,
            12,
        )
        .await
        .expect("start upload");
    foundation
        .store
        .append_log_frames(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(1, "upload recovery")],
            12,
        )
        .await
        .expect("task log");
    let original_commit: String = sqlx::query_scalar("SELECT commit_json FROM uploads WHERE id=?")
        .bind(upload.upload_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("commit JSON");
    let mut forged =
        serde_json::from_str::<PendingAttemptUpload>(&original_commit).expect("pending upload");
    forged.artifact.media_type = "text/html".into();
    sqlx::query("UPDATE uploads SET commit_json=? WHERE id=?")
        .bind(serde_json::to_string(&forged).expect("forged pending upload"))
        .bind(upload.upload_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("forge persisted media type");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: digest("unused"),
                    },
                    12,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query("UPDATE uploads SET commit_json=? WHERE id=?")
        .bind(original_commit)
        .bind(upload.upload_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("restore persisted upload");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .append_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    upload.upload_id,
                    0,
                    &digest("wrong"),
                    b"abc",
                    13,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
    let record = UploadRecord::new(
        upload.upload_id,
        &upload.artifact.name,
        &upload.artifact.relative_path,
        upload.artifact.size_bytes,
        upload.artifact.sha256.clone(),
        "original",
        100 * 1024 * 1024,
    )
    .expect("upload record");
    assert_eq!(
        artifacts
            .recovery_action(&record, true)
            .expect("ledger before first append"),
        RecoveryAction::ResumeReceiving
    );
    assert_eq!(
        artifacts
            .append_chunk(&record, 0, &digest("abc"), b"abc")
            .expect("fsync before cursor"),
        3
    );
    let cursor = foundation
        .store
        .append_attempt_upload(
            &artifacts,
            foundation.runner_id,
            upload.upload_id,
            0,
            &digest("abc"),
            b"abc",
            14,
        )
        .await
        .expect("reconcile file-ahead cursor");
    assert_eq!(cursor.received_bytes, 3);
    assert_eq!(
        foundation
            .store
            .append_attempt_upload(
                &artifacts,
                foundation.runner_id,
                upload.upload_id,
                0,
                &digest("abc"),
                b"abc",
                15,
            )
            .await
            .expect("duplicate chunk")
            .received_bytes,
        3
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .append_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    upload.upload_id,
                    0,
                    &digest("abd"),
                    b"abd",
                    16,
                )
                .await,
        ),
        ErrorCode::Conflict
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .verify_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    upload.upload_id,
                    &VerifyUploadRequest {
                        size_bytes: 3,
                        sha256: digest("bad"),
                    },
                    17,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
    sqlx::query("UPDATE uploads SET state='verified' WHERE id=?")
        .bind(upload.upload_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("verified before rename");
    let mut verified = record;
    verified
        .restore_progress(3, UploadState::Verified)
        .expect("verified record");
    artifacts
        .move_verified(&verified)
        .expect("rename before DB state");
    foundation
        .store
        .verify_attempt_upload(
            &artifacts,
            foundation.runner_id,
            upload.upload_id,
            &VerifyUploadRequest {
                size_bytes: 3,
                sha256: digest("abc"),
            },
            18,
        )
        .await
        .expect("rename crash converges");
    let resumed = foundation
        .store
        .start_attempt_upload(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &request,
            19,
        )
        .await
        .expect("resume same upload");
    assert_eq!(resumed.upload_id, upload.upload_id);
    assert_eq!(resumed.received_bytes, 3);
}

#[tokio::test]
async fn attempt_upload_uses_declared_file_name_and_supports_empty_files() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    let upload = foundation
        .store
        .start_attempt_upload(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &StartUploadRequest {
                name: "original".into(),
                media_type: "application/pdf".into(),
                size_bytes: 0,
                sha256: digest(""),
            },
            11,
        )
        .await
        .expect("start empty upload");
    assert!(upload.artifact.relative_path.ends_with("/source.pdf"));
    foundation
        .store
        .verify_attempt_upload(
            &artifacts,
            foundation.runner_id,
            upload.upload_id,
            &VerifyUploadRequest {
                size_bytes: 0,
                sha256: digest(""),
            },
            12,
        )
        .await
        .expect("verify empty upload");
    let path = database
        .directory
        .join("artifacts")
        .join(&upload.artifact.relative_path);
    assert_eq!(fs::metadata(path).expect("empty artifact").len(), 0);
    let state: String = sqlx::query_scalar("SELECT state FROM uploads WHERE id=?")
        .bind(upload.upload_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("upload state");
    assert_eq!(state, "moved");
}

#[tokio::test]
async fn attempt_upload_enforces_wildcard_names_counts_sizes_and_fence() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    let spec_json: String = sqlx::query_scalar("SELECT spec_json FROM tasks WHERE id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("spec");
    let mut spec: CompiledTaskSpec = serde_json::from_str(&spec_json).expect("strict spec");
    spec.artifacts.push(ArtifactDeclaration {
        name: "figures".into(),
        kind: ArtifactKind::Figure,
        path: "output/figures/*".into(),
        required: false,
        when: ArtifactWhen::OnSuccess,
        max_files: Some(1),
        max_bytes: 3,
    });
    sqlx::query("UPDATE tasks SET spec_json=? WHERE id=?")
        .bind(serde_json::to_string(&spec).expect("spec JSON"))
        .bind(foundation.task_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("wildcard declaration");
    for (name, size, expected) in [
        ("figures/../bad", 3, ErrorCode::ArtifactUndeclared),
        ("figures/large.png", 4, ErrorCode::ArtifactTooLarge),
    ] {
        assert_eq!(
            store_error_code(
                foundation
                    .store
                    .start_attempt_upload(
                        &artifacts,
                        foundation.runner_id,
                        claim.exec_id,
                        &StartUploadRequest {
                            name: name.into(),
                            media_type: "image/png".into(),
                            size_bytes: size,
                            sha256: digest("abc"),
                        },
                        11,
                    )
                    .await,
            ),
            expected
        );
    }
    foundation
        .store
        .start_attempt_upload(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &StartUploadRequest {
                name: "figures/one.png".into(),
                media_type: "image/png".into(),
                size_bytes: 3,
                sha256: digest("abc"),
            },
            12,
        )
        .await
        .expect("first wildcard");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "figures/two.png".into(),
                        media_type: "image/png".into(),
                        size_bytes: 3,
                        sha256: digest("abc"),
                    },
                    13,
                )
                .await,
        ),
        ErrorCode::ArtifactTooLarge
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    RunnerId::generate(),
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "original".into(),
                        media_type: "application/pdf".into(),
                        size_bytes: 3,
                        sha256: digest("abc"),
                    },
                    14,
                )
                .await,
        ),
        ErrorCode::StaleAttempt
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .start_attempt_upload(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &StartUploadRequest {
                        name: "original".into(),
                        media_type: "application/pdf".into(),
                        size_bytes: 3,
                        sha256: digest("abc"),
                    },
                    70,
                )
                .await,
        ),
        ErrorCode::LeaseExpired
    );
}

#[tokio::test]
async fn authenticated_completion_checks_required_usage_manifest_and_nas() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 100, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: manifest_digest(&foundation, claim.exec_id, Vec::new()),
                    },
                    11,
                )
                .await,
        ),
        ErrorCode::ArtifactUndeclared
    );
    let original = upload_bytes(
        &foundation,
        &artifacts,
        claim.exec_id,
        "original",
        "application/pdf",
        b"pdf",
        12,
    )
    .await;
    foundation
        .store
        .append_log_frames(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(1, "complete")],
            15,
        )
        .await
        .expect("task log");
    let expected = manifest_digest(&foundation, claim.exec_id, vec![&original]);
    sqlx::query(
        "INSERT INTO ai_usage(id,job_id,task_id,attempt_id,invocation_key,state,tool,model, \
         effort,created_at_ms) VALUES(?,?,?,?,?,'started','qoder_cli','test','high',18)",
    )
    .bind(AiUsageId::generate().to_string())
    .bind(foundation.job_id.to_string())
    .bind(foundation.task_id.to_string())
    .bind(claim.exec_id.to_string())
    .bind("open")
    .execute(&foundation.pool)
    .await
    .expect("open usage");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: expected.clone(),
                    },
                    19,
                )
                .await,
        ),
        ErrorCode::UsageConflict
    );
    sqlx::query("DELETE FROM ai_usage WHERE attempt_id=?")
        .bind(claim.exec_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("close usage fixture");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: digest("wrong"),
                    },
                    20,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
    let original_path = database
        .directory
        .join("artifacts")
        .join(&original.artifact.relative_path);
    fs::write(&original_path, b"bad").expect("mutate final artifact");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: expected.clone(),
                    },
                    21,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    fs::write(&original_path, b"pdf").expect("restore final artifact");
    let request = CompleteAttemptRequest {
        manifest_sha256: expected,
    };
    assert_eq!(
        foundation
            .store
            .complete_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &request,
                22,
            )
            .await
            .expect("complete")
            .state,
        AttemptState::Succeeded
    );
    sqlx::query(
        "UPDATE artifacts SET media_type='text/html' WHERE attempt_id=? AND name='original'",
    )
    .bind(claim.exec_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("forge committed media type");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &request,
                    23,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query(
        "UPDATE artifacts SET media_type='application/pdf' WHERE attempt_id=? AND name='original'",
    )
    .bind(claim.exec_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("restore committed media type");
    assert_eq!(
        foundation
            .store
            .complete_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &request,
                24,
            )
            .await
            .expect("idempotent complete")
            .state,
        AttemptState::Succeeded
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: digest("different"),
                    },
                    25,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
}

#[tokio::test]
async fn authenticated_failure_commits_only_always_artifacts() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 100, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    let original = upload_bytes(
        &foundation,
        &artifacts,
        claim.exec_id,
        "original",
        "application/pdf",
        b"pdf",
        11,
    )
    .await;
    let orphan: (
        String,
        String,
        String,
        String,
        String,
        String,
        i64,
        String,
        i64,
        String,
        i64,
        i64,
    ) = sqlx::query_as(
        "SELECT id,commit_json,name,target_id,staging_path,final_relative_path, \
             expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms \
             FROM uploads WHERE id=?",
    )
    .bind(original.upload_id.to_string())
    .fetch_one(&foundation.pool)
    .await
    .expect("discarded upload ledger");
    foundation
        .store
        .append_log_frames(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(1, "failed")],
            14,
        )
        .await
        .expect("task log");
    foundation
        .store
        .fail_authenticated_attempt(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &FailAttemptRequest {
                error_code: ErrorCode::ExecutorFailed,
                manifest_sha256: None,
            },
            17,
        )
        .await
        .expect("fail attempt");
    let names: Vec<String> =
        sqlx::query_scalar("SELECT name FROM artifacts WHERE attempt_id=? ORDER BY name")
            .bind(claim.exec_id.to_string())
            .fetch_all(&foundation.pool)
            .await
            .expect("committed artifacts");
    assert_eq!(names, vec!["log"]);
    let log_path: String =
        sqlx::query_scalar("SELECT relative_path FROM artifacts WHERE attempt_id=? AND name='log'")
            .bind(claim.exec_id.to_string())
            .fetch_one(&foundation.pool)
            .await
            .expect("log artifact path");
    assert!(
        !database
            .directory
            .join("artifacts")
            .join(original.artifact.relative_path)
            .exists()
    );
    assert!(database.directory.join("artifacts").join(log_path).exists());
    sqlx::query(
        "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
         final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state, \
         created_at_ms,updated_at_ms) VALUES(?,'attempt',?,?,?,?,?,?,?,?,?,?,?,?)",
    )
    .bind(&orphan.0)
    .bind(claim.exec_id.to_string())
    .bind(&orphan.1)
    .bind(&orphan.2)
    .bind(&orphan.3)
    .bind(&orphan.4)
    .bind(&orphan.5)
    .bind(orphan.6)
    .bind(&orphan.7)
    .bind(orphan.8)
    .bind(&orphan.9)
    .bind(orphan.10)
    .bind(orphan.11)
    .execute(&foundation.pool)
    .await
    .expect("simulate post-terminal cleanup crash");
    let orphan_path = database.directory.join("artifacts").join(&orphan.5);
    fs::write(&orphan_path, b"pdf").expect("restore discarded final file");
    let same = FailAttemptRequest {
        error_code: ErrorCode::ExecutorFailed,
        manifest_sha256: None,
    };
    assert_eq!(
        foundation
            .store
            .fail_authenticated_attempt(&artifacts, foundation.runner_id, claim.exec_id, &same, 18,)
            .await
            .expect("failed replay completes cleanup")
            .state,
        AttemptState::Failed
    );
    assert!(!orphan_path.exists());
    let orphan_count: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads WHERE id=?")
        .bind(&orphan.0)
        .fetch_one(&foundation.pool)
        .await
        .expect("orphan ledger count");
    assert_eq!(orphan_count, 0);
    assert_eq!(
        foundation
            .store
            .fail_authenticated_attempt(&artifacts, foundation.runner_id, claim.exec_id, &same, 19,)
            .await
            .expect("lost response replay")
            .state,
        AttemptState::Failed
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .fail_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &FailAttemptRequest {
                        error_code: ErrorCode::ExecutorFailed,
                        manifest_sha256: Some(digest("different")),
                    },
                    20,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .fail_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &FailAttemptRequest {
                        error_code: ErrorCode::AttemptTimeout,
                        manifest_sha256: None,
                    },
                    21,
                )
                .await,
        ),
        ErrorCode::Conflict
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: manifest_digest(&foundation, claim.exec_id, Vec::new(),),
                    },
                    22,
                )
                .await,
        ),
        ErrorCode::StaleAttempt
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
    let wrong_tool = UsageUpdate::Started {
        invocation_key: "wrong-tool".into(),
        tool: AiTool::CodexCli,
        model: "gpt-5.6".into(),
        effort: "high".into(),
    };
    assert_eq!(
        foundation
            .store
            .apply_usage_update(foundation.runner_id, exec_id, &wrong_tool, 11)
            .await
            .expect_err("runner did not register Codex")
            .code(),
        ErrorCode::UsageConflict
    );
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
    assert!(first.applied);
    assert!(!repeated.applied);
    assert_eq!(first.usage_id, repeated.usage_id);
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
    let wrong_metrics = UsageUpdate::Final {
        invocation_key: "note-1".into(),
        origin: UsageOrigin::Observed,
        input_tokens: Some(10),
        output_tokens: Some(20),
        cost_micros: None,
        credits_micros: Some(30),
    };
    assert_eq!(
        foundation
            .store
            .apply_usage_update(foundation.runner_id, exec_id, &wrong_metrics, 14)
            .await
            .expect_err("Qoder must not report token metrics")
            .code(),
        ErrorCode::UsageConflict
    );
    let final_update = UsageUpdate::Final {
        invocation_key: "note-1".into(),
        origin: UsageOrigin::Observed,
        input_tokens: None,
        output_tokens: None,
        cost_micros: None,
        credits_micros: Some(30),
    };
    let finalized = foundation
        .store
        .apply_usage_update(foundation.runner_id, exec_id, &final_update, 14)
        .await
        .expect("existing usage final may arrive late");
    assert_eq!(finalized.state, AiUsageState::Final);
    assert!(finalized.applied);
    let replayed = foundation
        .store
        .apply_usage_update(foundation.runner_id, exec_id, &final_update, 15)
        .await
        .expect("same final usage is idempotent");
    assert!(!replayed.applied);
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
                16,
            )
            .await
            .expect_err("new usage cannot start late")
            .code(),
        ErrorCode::StaleAttempt
    );
    assert_eq!(
        foundation
            .store
            .apply_usage_update(RunnerId::generate(), exec_id, &final_update, 17)
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

async fn upload_bytes(
    foundation: &Foundation,
    artifacts: &NasArtifactStore,
    attempt_id: AttemptId,
    name: &str,
    media_type: &str,
    bytes: &[u8],
    now_ms: i64,
) -> StartUploadResponse {
    let sha256 = digest(std::str::from_utf8(bytes).expect("test bytes are UTF-8"));
    let upload = foundation
        .store
        .start_attempt_upload(
            artifacts,
            foundation.runner_id,
            attempt_id,
            &StartUploadRequest {
                name: name.into(),
                media_type: media_type.into(),
                size_bytes: bytes.len() as u64,
                sha256: sha256.clone(),
            },
            now_ms,
        )
        .await
        .expect("start upload");
    foundation
        .store
        .append_attempt_upload(
            artifacts,
            foundation.runner_id,
            upload.upload_id,
            0,
            &sha256,
            bytes,
            now_ms + 1,
        )
        .await
        .expect("append upload");
    foundation
        .store
        .verify_attempt_upload(
            artifacts,
            foundation.runner_id,
            upload.upload_id,
            &VerifyUploadRequest {
                size_bytes: bytes.len() as u64,
                sha256,
            },
            now_ms + 2,
        )
        .await
        .expect("verify upload");
    upload
}

fn manifest_digest(
    foundation: &Foundation,
    attempt_id: AttemptId,
    mut uploads: Vec<&StartUploadResponse>,
) -> Sha256Digest {
    uploads.sort_by(|left, right| left.artifact.name.cmp(&right.artifact.name));
    let manifest = ArtifactManifest::new(
        foundation.job_id,
        foundation.task_id,
        attempt_id,
        uploads
            .into_iter()
            .map(|upload| upload.artifact.clone())
            .collect(),
    );
    digest(&serde_json::to_string(&manifest).expect("manifest"))
}

fn frame(sequence: u64, message: &str) -> LogFrame {
    let line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: sequence,
        level: TaskLogLevel::Info,
        message: message.into(),
    })
    .expect("task log line");
    LogFrame {
        sequence,
        sha256: digest(&line),
        line,
    }
}

fn store_error_code<T>(result: Result<T, flori_store::StoreError>) -> ErrorCode {
    match result {
        Err(error) => error.code(),
        Ok(_) => panic!("expected Store error"),
    }
}

async fn attach_credential(
    pool: &SqlitePool,
    source_id: flori_core::SourceId,
    kind: &str,
    value: &str,
) {
    let credential_id = CredentialId::generate();
    sqlx::query(
        "INSERT INTO credentials(id,kind,name,plaintext_value,created_at_ms,updated_at_ms) \
         VALUES(?,?,?,?,2,2)",
    )
    .bind(credential_id.to_string())
    .bind(kind)
    .bind(format!("credential-{credential_id}"))
    .bind(value)
    .execute(pool)
    .await
    .expect("credential");
    sqlx::query("UPDATE sources SET credential_id=? WHERE id=?")
        .bind(credential_id.to_string())
        .bind(source_id.to_string())
        .execute(pool)
        .await
        .expect("attach credential");
}
