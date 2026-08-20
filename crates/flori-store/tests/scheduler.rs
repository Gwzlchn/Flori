use std::{fmt::Write, fs, path::PathBuf, sync::Arc};

use flori_core::{
    ArtifactManifest, AttemptId, AttemptState, CollectionId, CompiledTaskSpec,
    CompleteAttemptRequest, CreateJobRequest, DomainId, ErrorCode, Executor, FailAttemptRequest,
    JobId, JobInputs, JobTrigger, PipelineId, PipelineRevisionId, PromptSnapshot, PromptSnapshotId,
    PromptSnapshotProfile, PromptSnapshotPrompt, RunnerId, Sha256Digest, SourceId, SourceKind,
    TaskId, TaskInputBindings, TaskInputReference, TaskState,
};
use flori_pipeline::compile;
use flori_store::{CreateJob, CreateSource, Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

struct TestDatabase {
    directory: PathBuf,
    path: PathBuf,
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("write digest");
    }
    Sha256Digest::parse(output).expect("digest")
}

impl TestDatabase {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("flori-wp08-{}", JobId::generate()));
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
    source_id: SourceId,
    revision_id: PipelineRevisionId,
    runner_id: RunnerId,
}

struct JobTasks {
    job_id: JobId,
    work_id: TaskId,
    validate_id: TaskId,
    publish_id: TaskId,
}

async fn seed_foundation(pool: &SqlitePool) -> Foundation {
    let domain_id = DomainId::generate();
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    let source_id = SourceId::generate();
    let runner_id = RunnerId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
        .bind(domain_id.to_string()).bind(format!("domain-{domain_id}")).bind("Domain")
        .execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
        .bind(pipeline_id.to_string())
        .bind(format!("pipeline-{pipeline_id}"))
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'work: {}',0)")
        .bind(revision_id.to_string()).bind(pipeline_id.to_string()).bind("0".repeat(64))
        .execute(pool).await.expect("revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_url',?,?,?,?,0,0)")
        .bind(source_id.to_string()).bind(format!("https://example.test/{source_id}.pdf"))
        .bind(domain_id.to_string()).bind(format!("source-{source_id}")).bind("1".repeat(64))
        .execute(pool).await.expect("source");
    sqlx::query("INSERT INTO runners(id,name,state,config_revision,max_concurrency,tags_json,tools_json,ai_models_json,created_at_ms,updated_at_ms) VALUES(?,?,'enabled',1,2,'[]','[]','[]',0,0)")
        .bind(runner_id.to_string()).bind(format!("runner-{runner_id}"))
        .execute(pool).await.expect("runner");
    Foundation {
        source_id,
        revision_id,
        runner_id,
    }
}

async fn insert_job(pool: &SqlitePool, foundation: &Foundation, ordinal: u8) -> JobTasks {
    let job_id = JobId::generate();
    let work_id = TaskId::generate();
    let validate_id = TaskId::generate();
    let publish_id = TaskId::generate();
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms) VALUES(?,?,?,'pipeline_rerun','queued',?,?,'{}','{\"translate\":false}',?,?,?)")
        .bind(job_id.to_string()).bind(foundation.source_id.to_string()).bind(foundation.revision_id.to_string())
        .bind(JobId::generate().to_string()).bind("2".repeat(64)).bind(format!("job-{job_id}"))
        .bind("3".repeat(64)).bind(i64::from(ordinal))
        .execute(pool).await.expect("job");
    let spec = CompiledTaskSpec {
        executor: Executor::DocumentAcquire,
        needs: Vec::new(),
        tags: Vec::new(),
        retry: 1,
        timeout_ms: 1_000,
        artifacts: Vec::new(),
    };
    let bindings = TaskInputBindings::DocumentAcquire {
        source: TaskInputReference::Source,
    };
    sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms,ready_at_ms) VALUES(?,?,'work','document.acquire',?,?,'ready',2,1000,0)")
        .bind(work_id.to_string()).bind(job_id.to_string())
        .bind(serde_json::to_string(&spec).expect("work spec"))
        .bind(serde_json::to_string(&bindings).expect("work bindings"))
        .execute(pool).await.expect("work task");
    sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms) VALUES(?,?,'validate','core.validate',?,'{}','pending',1,1000)")
        .bind(validate_id.to_string()).bind(job_id.to_string()).bind(r#"{"executor":"core.validate","needs":["work"],"tags":[],"retry":0,"timeout_ms":1000,"artifacts":[]}"#)
        .execute(pool).await.expect("validate task");
    sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms) VALUES(?,?,'publish','core.publish',?,'{}','pending',1,1000)")
        .bind(publish_id.to_string()).bind(job_id.to_string()).bind(r#"{"executor":"core.publish","needs":["validate"],"tags":[],"retry":0,"timeout_ms":1000,"artifacts":[]}"#)
        .execute(pool).await.expect("publish task");
    JobTasks {
        job_id,
        work_id,
        validate_id,
        publish_id,
    }
}

async fn execute_and_publish(
    store: &Store,
    artifacts: &NasArtifactStore,
    foundation: &Foundation,
    tasks: &JobTasks,
    now_ms: i64,
) {
    let attempt_id = AttemptId::generate();
    store
        .lease_task(
            tasks.work_id,
            attempt_id,
            foundation.runner_id,
            now_ms,
            now_ms + 50,
        )
        .await
        .expect("lease work");
    let completion = completion(tasks, attempt_id);
    assert_eq!(
        store
            .complete_authenticated_attempt(
                artifacts,
                foundation.runner_id,
                attempt_id,
                &completion,
                now_ms + 1,
            )
            .await
            .expect("complete work")
            .state,
        AttemptState::Succeeded
    );
    assert_eq!(
        store
            .complete_authenticated_attempt(
                artifacts,
                foundation.runner_id,
                attempt_id,
                &completion,
                now_ms + 1,
            )
            .await
            .expect("idempotent completion")
            .state,
        AttemptState::Succeeded
    );
    assert_eq!(
        store
            .complete_core_task(
                tasks.job_id,
                tasks.validate_id,
                AttemptId::generate(),
                now_ms + 2,
            )
            .await
            .expect("complete core validation"),
        TaskState::Succeeded
    );
    store
        .publish_job(
            tasks.job_id,
            tasks.publish_id,
            AttemptId::generate(),
            now_ms + 3,
        )
        .await
        .expect("publish job");
}

fn completion(tasks: &JobTasks, attempt_id: AttemptId) -> CompleteAttemptRequest {
    let manifest = ArtifactManifest::new(tasks.job_id, tasks.work_id, attempt_id, Vec::new());
    CompleteAttemptRequest {
        manifest_sha256: digest(&serde_json::to_string(&manifest).expect("manifest")),
    }
}

#[tokio::test]
async fn dag_progress_and_publish_keep_only_current_and_previous_pointers() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifacts");
    let pool = database.pool().await;
    let foundation = seed_foundation(&pool).await;
    let first = insert_job(&pool, &foundation, 1).await;
    let second = insert_job(&pool, &foundation, 2).await;
    let third = insert_job(&pool, &foundation, 3).await;

    execute_and_publish(&store, &artifacts, &foundation, &first, 100).await;
    execute_and_publish(&store, &artifacts, &foundation, &second, 200).await;
    execute_and_publish(&store, &artifacts, &foundation, &third, 300).await;
    store
        .publish_job(third.job_id, third.publish_id, AttemptId::generate(), 302)
        .await
        .expect("idempotent publish");

    let pointers: (Option<String>, Option<String>) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(foundation.source_id.to_string())
            .fetch_one(&pool)
            .await
            .expect("pointers");
    assert_eq!(
        pointers,
        (
            Some(third.job_id.to_string()),
            Some(second.job_id.to_string())
        )
    );
    let first_state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
        .bind(first.job_id.to_string())
        .fetch_one(&pool)
        .await
        .expect("old job remains audit");
    assert_eq!(first_state, "succeeded");
}

#[tokio::test]
async fn transient_retry_and_terminal_failure_fence_late_attempts() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifacts");
    let pool = database.pool().await;
    let foundation = seed_foundation(&pool).await;
    let tasks = insert_job(&pool, &foundation, 1).await;
    let first = AttemptId::generate();
    store
        .lease_task(tasks.work_id, first, foundation.runner_id, 10, 100)
        .await
        .expect("first lease");
    assert_eq!(
        store
            .fail_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                first,
                &FailAttemptRequest {
                    error_code: ErrorCode::NetworkTemporary,
                    manifest_sha256: None,
                },
                20,
            )
            .await
            .expect("retry")
            .state,
        AttemptState::Failed
    );
    let second = AttemptId::generate();
    store
        .lease_task(tasks.work_id, second, foundation.runner_id, 21, 100)
        .await
        .expect("second lease");
    assert_eq!(
        store
            .fail_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                second,
                &FailAttemptRequest {
                    error_code: ErrorCode::ExecutorFailed,
                    manifest_sha256: None,
                },
                22,
            )
            .await
            .expect("terminal failure")
            .state,
        AttemptState::Failed
    );

    let late = store
        .complete_authenticated_attempt(
            &artifacts,
            foundation.runner_id,
            first,
            &completion(&tasks, first),
            23,
        )
        .await
        .expect_err("old fence rejected");
    assert_eq!(late.code(), ErrorCode::StaleAttempt);
    let states: (String, String, String) = sqlx::query_as(
        "SELECT j.state,w.state,p.state FROM jobs j JOIN tasks w ON w.job_id=j.id AND w.task_key='work' JOIN tasks p ON p.job_id=j.id AND p.task_key='publish' WHERE j.id=?",
    ).bind(tasks.job_id.to_string()).fetch_one(&pool).await.expect("terminal states");
    assert_eq!(
        states,
        ("failed".into(), "failed".into(), "canceled".into())
    );
}

#[tokio::test]
async fn concurrent_claim_has_exactly_one_winner() {
    let database = TestDatabase::new();
    let store = Arc::new(Store::open(&database.path).await.expect("store"));
    let pool = database.pool().await;
    let foundation = seed_foundation(&pool).await;
    let tasks = insert_job(&pool, &foundation, 1).await;
    let (left, right) = tokio::join!(
        store.lease_task(
            tasks.work_id,
            AttemptId::generate(),
            foundation.runner_id,
            10,
            100
        ),
        store.lease_task(
            tasks.work_id,
            AttemptId::generate(),
            foundation.runner_id,
            10,
            100
        ),
    );
    assert_eq!(usize::from(left.is_ok()) + usize::from(right.is_ok()), 1);
}

#[tokio::test]
async fn expired_attempt_retries_once_then_fails_the_job() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let pool = database.pool().await;
    let foundation = seed_foundation(&pool).await;
    let tasks = insert_job(&pool, &foundation, 1).await;
    let first = AttemptId::generate();
    store
        .lease_task(tasks.work_id, first, foundation.runner_id, 10, 20)
        .await
        .expect("first lease");
    assert_eq!(
        store
            .expire_attempt(first, ErrorCode::RunnerLost, 20)
            .await
            .expect("retry expired attempt"),
        TaskState::Ready
    );
    let second = AttemptId::generate();
    store
        .lease_task(tasks.work_id, second, foundation.runner_id, 21, 30)
        .await
        .expect("second lease");
    assert_eq!(
        store
            .expire_attempt(second, ErrorCode::AttemptTimeout, 30)
            .await
            .expect("attempt budget exhausted"),
        TaskState::Failed
    );
    let attempt_states: Vec<String> =
        sqlx::query_scalar("SELECT state FROM attempts WHERE task_id=? ORDER BY attempt_no")
            .bind(tasks.work_id.to_string())
            .fetch_all(&pool)
            .await
            .expect("attempt states");
    assert_eq!(attempt_states, ["expired", "expired"]);
}

#[tokio::test]
async fn compiled_pipeline_materializes_strict_tasks_and_pipeline_rerun() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let pool = database.pool().await;
    let domain_id = DomainId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'profile',0,0)")
        .bind(domain_id.to_string()).bind("papers").bind("Papers")
        .execute(&pool).await.expect("domain");
    let collection_id = CollectionId::generate();
    sqlx::query("INSERT INTO collections(id,domain_id,name,kind,enabled,created_at_ms,updated_at_ms) VALUES(?,?,'Reading','manual',1,0,0)")
        .bind(collection_id.to_string()).bind(domain_id.to_string())
        .execute(&pool).await.expect("collection");
    sqlx::query("INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('document_note','write a note',?,0)")
        .bind(digest("write a note").as_str()).execute(&pool).await.expect("prompt");
    let yaml = include_bytes!("../../../pipelines/pdf.yml");
    let compilation = compile("pdf", yaml).expect("compile frozen PDF pipeline");
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    let registered = store
        .register_pipeline_revision(
            pipeline_id,
            revision_id,
            &compilation,
            "deadbeef",
            std::str::from_utf8(yaml).expect("UTF-8"),
            1,
        )
        .await
        .expect("register pipeline");
    assert_eq!(registered, revision_id);
    assert_eq!(
        store
            .register_pipeline_revision(
                pipeline_id,
                PipelineRevisionId::generate(),
                &compilation,
                "deadbeef",
                std::str::from_utf8(yaml).expect("UTF-8"),
                2,
            )
            .await
            .expect("same digest reuses revision"),
        revision_id,
    );

    let source_request = CreateSource {
        kind: SourceKind::PdfUrl,
        canonical_ref: "https://example.test/paper.pdf",
        title: Some("Paper"),
        domain_id,
        collection_ids: &[collection_id],
        request_key: "source-request",
        request_sha256: &"1".repeat(64),
        created_at_ms: 3,
    };
    let source_id = store.create_source(source_request).await.expect("source");
    let membership: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM collection_sources WHERE collection_id=? AND source_id=?",
    )
    .bind(collection_id.to_string())
    .bind(source_id.to_string())
    .fetch_one(&pool)
    .await
    .expect("collection membership");
    assert_eq!(membership, 1);
    assert_eq!(
        store
            .create_source(source_request)
            .await
            .expect("idempotent source"),
        source_id
    );
    let prompt_snapshot = PromptSnapshot {
        profile: PromptSnapshotProfile {
            domain_id,
            profile_text: "profile".into(),
            sha256: digest("profile"),
        },
        prompts: vec![PromptSnapshotPrompt {
            key: "document_note".into(),
            content: "write a note".into(),
            sha256: digest("write a note"),
        }],
    };
    let requested = CreateJobRequest {
        request_key: "job-initial".into(),
        pipeline_id,
        inputs: JobInputs { translate: false },
    };
    let first_job = store
        .create_requested_job(source_id, &requested, 4)
        .await
        .expect("requested initial job");
    assert_eq!(
        store
            .create_requested_job(source_id, &requested, 4)
            .await
            .expect("idempotent requested job"),
        first_job
    );
    let initial = CreateJob {
        source_id,
        pipeline_revision_id: revision_id,
        trigger: JobTrigger::Initial,
        rerun_of_job_id: None,
        prompt_snapshot_id: PromptSnapshotId::generate(),
        prompt_snapshot: &prompt_snapshot,
        request_key: "job-low-level",
        request_sha256: &"3".repeat(64),
        inputs: JobInputs { translate: false },
        created_at_ms: 4,
    };
    let states: Vec<(String, String)> =
        sqlx::query_as("SELECT task_key,state FROM tasks WHERE job_id=? ORDER BY task_key")
            .bind(first_job.to_string())
            .fetch_all(&pool)
            .await
            .expect("materialized tasks");
    assert_eq!(states.len(), compilation.pipeline.tasks.len());
    assert!(states.contains(&("acquire".into(), "ready".into())));
    assert!(states.contains(&("translate".into(), "skipped".into())));
    let stored_snapshot: (String, String, String) = sqlx::query_as(
        "SELECT prompt_snapshot_json,prompt_snapshot_sha256,inputs_json FROM jobs WHERE id=?",
    )
    .bind(first_job.to_string())
    .fetch_one(&pool)
    .await
    .expect("stored prompt snapshot");
    assert_eq!(
        serde_json::from_str::<PromptSnapshot>(&stored_snapshot.0).expect("strict prompt snapshot"),
        prompt_snapshot
    );
    assert_eq!(stored_snapshot.1, digest(&stored_snapshot.0).as_str());
    assert_eq!(
        serde_json::from_str::<JobInputs>(&stored_snapshot.2).expect("strict job inputs"),
        requested.inputs
    );
    let frozen_tasks: Vec<(String, String)> = sqlx::query_as(
        "SELECT spec_json,input_bindings_json FROM tasks WHERE job_id=? ORDER BY task_key",
    )
    .bind(first_job.to_string())
    .fetch_all(&pool)
    .await
    .expect("frozen task types");
    for (spec_json, bindings_json) in frozen_tasks {
        let spec = serde_json::from_str::<CompiledTaskSpec>(&spec_json).expect("strict task spec");
        let bindings = serde_json::from_str::<TaskInputBindings>(&bindings_json)
            .expect("strict input bindings");
        assert!(bindings.is_valid());
        assert_eq!(spec.executor, bindings.executor());
    }

    let mut invalid_profile = prompt_snapshot.clone();
    invalid_profile.profile.sha256 = digest("wrong profile");
    let invalid = CreateJob {
        prompt_snapshot: &invalid_profile,
        request_key: "job-invalid-profile",
        request_sha256: &"4".repeat(64),
        ..initial
    };
    assert_eq!(
        store
            .create_job(invalid, &compilation)
            .await
            .expect_err("profile digest must match")
            .code(),
        ErrorCode::InvalidRequest
    );
    let mut invalid_content = prompt_snapshot.clone();
    invalid_content.prompts[0].sha256 = digest("wrong content");
    let invalid = CreateJob {
        prompt_snapshot: &invalid_content,
        request_key: "job-invalid-content",
        request_sha256: &"4".repeat(64),
        ..initial
    };
    assert_eq!(
        store
            .create_job(invalid, &compilation)
            .await
            .expect_err("prompt content digest must match")
            .code(),
        ErrorCode::InvalidRequest
    );
    let unsorted_prompts = PromptSnapshot {
        profile: prompt_snapshot.profile.clone(),
        prompts: vec![
            PromptSnapshotPrompt {
                key: "document_translate".into(),
                content: "translate".into(),
                sha256: digest("translate"),
            },
            prompt_snapshot.prompts[0].clone(),
        ],
    };
    let invalid = CreateJob {
        prompt_snapshot: &unsorted_prompts,
        request_key: "job-unsorted-prompts",
        request_sha256: &"4".repeat(64),
        inputs: JobInputs { translate: true },
        ..initial
    };
    assert_eq!(
        store
            .create_job(invalid, &compilation)
            .await
            .expect_err("prompt keys must be sorted and unique")
            .code(),
        ErrorCode::InvalidRequest
    );

    let busy = CreateJob {
        request_key: "job-busy",
        request_sha256: &"4".repeat(64),
        ..initial
    };
    assert_eq!(
        store
            .create_job(busy, &compilation)
            .await
            .expect_err("one active job per source")
            .code(),
        ErrorCode::SourceBusy
    );
    sqlx::query("UPDATE jobs SET state='failed',finished_at_ms=5 WHERE id=?")
        .bind(first_job.to_string())
        .execute(&pool)
        .await
        .expect("finish first job");
    sqlx::query("UPDATE tasks SET state='canceled',finished_at_ms=5 WHERE job_id=? AND state IN ('pending','ready')")
        .bind(first_job.to_string()).execute(&pool).await.expect("finish first tasks");
    let rerun = CreateJob {
        trigger: JobTrigger::PipelineRerun,
        rerun_of_job_id: Some(first_job),
        prompt_snapshot_id: PromptSnapshotId::generate(),
        request_key: "job-rerun",
        request_sha256: &"5".repeat(64),
        created_at_ms: 6,
        ..initial
    };
    let rerun_job = store
        .create_job(rerun, &compilation)
        .await
        .expect("pipeline rerun");
    assert_ne!(rerun_job, first_job);
    let shared_ids: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM tasks old JOIN tasks new ON old.id=new.id WHERE old.job_id=? AND new.job_id=?",
    ).bind(first_job.to_string()).bind(rerun_job.to_string()).fetch_one(&pool).await.expect("new task IDs");
    assert_eq!(shared_ids, 0);
}
