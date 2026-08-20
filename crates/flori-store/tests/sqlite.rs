use std::{fs, path::PathBuf};

use flori_core::{
    AiTool, AiUsageId, AttemptId, DomainId, ErrorCode, JobId, PipelineId, PipelineRevisionId,
    RunnerId, SourceId, TaskId, UsageOrigin,
};
use flori_store::{FinalAiUsage, StartAiUsage, Store};
use sqlx::{Connection, Executor, SqliteConnection, SqlitePool, sqlite::SqliteConnectOptions};

struct TestDatabase {
    directory: PathBuf,
    path: PathBuf,
}

impl TestDatabase {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("flori-wp05-{}", JobId::generate()));
        fs::create_dir(&directory).expect("create isolated test directory");
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
        .expect("connect to test database")
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove isolated test directory");
    }
}

struct Seed {
    job_id: JobId,
    task_id: TaskId,
    runner_id: RunnerId,
}

async fn seed_ready_ai_task(pool: &SqlitePool) -> Seed {
    let domain_id = DomainId::generate();
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    let source_id = SourceId::generate();
    let job_id = JobId::generate();
    let task_id = TaskId::generate();
    let runner_id = RunnerId::generate();

    sqlx::query(
        "INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms)\
         VALUES(?,?,?,'',0,0)",
    )
    .bind(domain_id.to_string())
    .bind(format!("domain-{domain_id}"))
    .bind("Domain")
    .execute(pool)
    .await
    .expect("seed domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
        .bind(pipeline_id.to_string())
        .bind(format!("pipeline-{pipeline_id}"))
        .execute(pool)
        .await
        .expect("seed pipeline");
    sqlx::query(
        "INSERT INTO pipeline_revisions(\
           id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms\
         ) VALUES(?,?,1,'0123456',?,'tasks: {}',0)",
    )
    .bind(revision_id.to_string())
    .bind(pipeline_id.to_string())
    .bind("0".repeat(64))
    .execute(pool)
    .await
    .expect("seed pipeline revision");
    sqlx::query(
        "INSERT INTO runners(\
           id,name,state,config_revision,max_concurrency,tags_json,tools_json,ai_models_json,\
           created_at_ms,updated_at_ms\
         ) VALUES(?,?,'enabled',1,1,'[]','[]','[]',0,0)",
    )
    .bind(runner_id.to_string())
    .bind(format!("runner-{runner_id}"))
    .execute(pool)
    .await
    .expect("seed runner");
    sqlx::query(
        "INSERT INTO sources(\
           id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms\
         ) VALUES(?,'pdf_url',?,?,?, ?,0,0)",
    )
    .bind(source_id.to_string())
    .bind(format!("https://example.test/{source_id}.pdf"))
    .bind(domain_id.to_string())
    .bind(format!("source-{source_id}"))
    .bind("1".repeat(64))
    .execute(pool)
    .await
    .expect("seed source");
    sqlx::query(
        "INSERT INTO jobs(\
           id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,\
           prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,\
           created_at_ms,started_at_ms\
         ) VALUES(?,?,?,'initial','queued',?,?, '{}','{\"translate\":false}',?,?,0,NULL)",
    )
    .bind(job_id.to_string())
    .bind(source_id.to_string())
    .bind(revision_id.to_string())
    .bind(JobId::generate().to_string())
    .bind("2".repeat(64))
    .bind(format!("job-{job_id}"))
    .bind("3".repeat(64))
    .execute(pool)
    .await
    .expect("seed job");
    sqlx::query(
        "INSERT INTO tasks(\
           id,job_id,task_key,executor,spec_json,input_bindings_json,state,\
           selected_model,selected_effort,runner_config_revision,attempt_limit,timeout_ms,ready_at_ms\
         ) VALUES(?,?,'note','ai.document_note','{}','{}','ready','model','medium',1,3,1000,0)",
    )
    .bind(task_id.to_string())
    .bind(job_id.to_string())
    .execute(pool)
    .await
    .expect("seed task");
    Seed {
        job_id,
        task_id,
        runner_id,
    }
}

#[tokio::test]
async fn empty_directory_creates_exact_current_schema_and_reopens() {
    let database = TestDatabase::new();
    assert!(!database.path.exists());
    let store = Store::open(&database.path).await.expect("create v1 store");
    let pool = database.pool().await;

    let business_tables: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name IN (\
         'schema_meta','pipelines','pipeline_revisions','sources','source_inputs','jobs','tasks',\
         'attempts','uploads','artifacts','runners','credentials','prompts','ai_usage','job_events',\
         'domains','collections','collection_sources','glossary_terms','concept_occurrences',\
         'concept_edges','evidence','search_chunks','search_chunk_evidence')",
    )
    .fetch_one(&pool)
    .await
    .expect("count business tables");
    let migration_table: i64 =
        sqlx::query_scalar("SELECT count(*) FROM sqlite_schema WHERE name='_sqlx_migrations'")
            .fetch_one(&pool)
            .await
            .expect("check migration table");
    let journal_mode: String = sqlx::query_scalar("PRAGMA journal_mode")
        .fetch_one(&pool)
        .await
        .expect("read journal mode");
    let foreign_keys: i64 = sqlx::query_scalar("PRAGMA foreign_keys")
        .fetch_one(&pool)
        .await
        .expect("read foreign key mode");
    let integrity: String = sqlx::query_scalar("PRAGMA integrity_check")
        .fetch_one(&pool)
        .await
        .expect("check persisted database integrity");
    let foreign_key_errors = sqlx::query("PRAGMA foreign_key_check")
        .fetch_all(&pool)
        .await
        .expect("check persisted foreign keys");
    assert_eq!(business_tables, 24);
    assert_eq!(migration_table, 0);
    assert_eq!(journal_mode, "wal");
    assert_eq!(foreign_keys, 1);
    assert_eq!(integrity, "ok");
    assert!(foreign_key_errors.is_empty());

    pool.close().await;
    drop(store);
    Store::open(&database.path)
        .await
        .expect("reopen unchanged v1 store");
}

#[tokio::test]
async fn malformed_or_unknown_schema_is_not_rewritten() {
    let database = TestDatabase::new();
    let mut connection = SqliteConnection::connect_with(
        &SqliteConnectOptions::new()
            .filename(&database.path)
            .create_if_missing(true),
    )
    .await
    .expect("create malformed database");
    connection
        .execute("CREATE TABLE sentinel(value TEXT NOT NULL); INSERT INTO sentinel VALUES('keep')")
        .await
        .expect("seed malformed schema");
    let before: Vec<(String, String)> =
        sqlx::query_as("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
            .fetch_all(&mut connection)
            .await
            .expect("snapshot malformed schema");
    connection.close().await.expect("close malformed database");

    let error = match Store::open(&database.path).await {
        Ok(_) => panic!("unknown schema must fail closed"),
        Err(error) => error,
    };
    assert_eq!(error.code(), ErrorCode::SchemaMismatch);

    let pool = database.pool().await;
    let after: Vec<(String, String)> =
        sqlx::query_as("SELECT name,sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name")
            .fetch_all(&pool)
            .await
            .expect("read malformed schema after rejected open");
    let sentinel: String = sqlx::query_scalar("SELECT value FROM sentinel")
        .fetch_one(&pool)
        .await
        .expect("sentinel remains");
    assert_eq!(after, before);
    assert_eq!(sentinel, "keep");
}

#[tokio::test]
async fn lease_compare_and_swap_expiry_and_fence_are_transactional() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("create store");
    let pool = database.pool().await;
    let seed = seed_ready_ai_task(&pool).await;
    let first = AttemptId::generate();
    let competing = AttemptId::generate();
    sqlx::query("UPDATE tasks SET attempt_limit=4 WHERE id=?")
        .bind(seed.task_id.to_string())
        .execute(&pool)
        .await
        .expect_err("attempt limit above three is rejected");
    sqlx::query("UPDATE tasks SET timeout_ms=999 WHERE id=?")
        .bind(seed.task_id.to_string())
        .execute(&pool)
        .await
        .expect_err("timeout below one second is rejected");

    store
        .lease_task(seed.task_id, first, seed.runner_id, 100, 200)
        .await
        .expect("lease ready task");
    let job_after_claim: (String, Option<i64>) =
        sqlx::query_as("SELECT state,started_at_ms FROM jobs WHERE id=?")
            .bind(seed.job_id.to_string())
            .fetch_one(&pool)
            .await
            .expect("read parent job after first claim");
    assert_eq!(job_after_claim, ("running".to_owned(), Some(100)));
    let rejected = store
        .lease_task(seed.task_id, competing, seed.runner_id, 100, 200)
        .await
        .expect_err("CAS rejects a competing lease");
    assert_eq!(rejected.code(), ErrorCode::Conflict);
    store
        .renew_lease(first, seed.runner_id, 150, 300)
        .await
        .expect("renew live current lease");
    store
        .renew_lease(first, seed.runner_id, 160, 300)
        .await
        .expect("repeated renewal with same expiry is idempotent");
    let expired = store
        .renew_lease(first, seed.runner_id, 301, 400)
        .await
        .expect_err("expired lease cannot renew");
    assert_eq!(expired.code(), ErrorCode::LeaseExpired);

    sqlx::query("UPDATE attempts SET state='expired',finished_at_ms=301 WHERE id=?")
        .bind(first.to_string())
        .execute(&pool)
        .await
        .expect("expire first attempt");
    sqlx::query("UPDATE tasks SET state='ready',ready_at_ms=301 WHERE id=?")
        .bind(seed.task_id.to_string())
        .execute(&pool)
        .await
        .expect("requeue task");
    store
        .lease_task(seed.task_id, competing, seed.runner_id, 302, 500)
        .await
        .expect("lease requeued task with new fence");
    let stale = store
        .renew_lease(first, seed.runner_id, 303, 600)
        .await
        .expect_err("old attempt cannot cross new fence");
    assert_eq!(stale.code(), ErrorCode::StaleAttempt);
}

#[tokio::test]
async fn usage_is_idempotent_conflict_safe_and_survives_restart() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("create store");
    let pool = database.pool().await;
    let seed = seed_ready_ai_task(&pool).await;
    let attempt_id = AttemptId::generate();
    store
        .lease_task(seed.task_id, attempt_id, seed.runner_id, 100, 200)
        .await
        .expect("lease AI task");

    let usage_id = AiUsageId::generate();
    let start = StartAiUsage {
        id: usage_id,
        job_id: seed.job_id,
        task_id: seed.task_id,
        attempt_id,
        invocation_key: "call-1",
        tool: AiTool::CodexCli,
        model: "model",
        effort: "medium",
        created_at_ms: 110,
    };
    let started = store
        .start_ai_usage(start, 110)
        .await
        .expect("write started usage");
    assert_eq!(started.id, usage_id);
    assert!(started.applied);
    let duplicate = store
        .start_ai_usage(
            StartAiUsage {
                id: AiUsageId::generate(),
                ..start
            },
            120,
        )
        .await
        .expect("same start returns original row");
    assert_eq!(duplicate.id, usage_id);
    assert!(!duplicate.applied);

    sqlx::query("UPDATE attempts SET state='expired',finished_at_ms=201 WHERE id=?")
        .bind(attempt_id.to_string())
        .execute(&pool)
        .await
        .expect("expire attempt");
    sqlx::query("UPDATE tasks SET state='ready',ready_at_ms=201 WHERE id=?")
        .bind(seed.task_id.to_string())
        .execute(&pool)
        .await
        .expect("requeue AI task");
    store
        .lease_task(
            seed.task_id,
            AttemptId::generate(),
            seed.runner_id,
            202,
            400,
        )
        .await
        .expect("new attempt establishes a later fence");
    let late_new_usage = store
        .start_ai_usage(
            StartAiUsage {
                id: AiUsageId::generate(),
                invocation_key: "call-late",
                ..start
            },
            220,
        )
        .await
        .expect_err("late attempt cannot start new usage");
    assert_eq!(late_new_usage.code(), ErrorCode::StaleAttempt);

    let final_usage = FinalAiUsage {
        attempt_id,
        invocation_key: "call-1",
        origin: UsageOrigin::Observed,
        input_tokens: Some(10),
        output_tokens: Some(20),
        cost_micros: Some(30),
        credits_micros: None,
        finalized_at_ms: 230,
    };
    store
        .finalize_ai_usage(final_usage)
        .await
        .expect("existing started usage can finalize after fence");
    pool.close().await;
    drop(store);
    let reopened = Store::open(&database.path).await.expect("reopen store");
    let duplicate_final = reopened
        .finalize_ai_usage(FinalAiUsage {
            finalized_at_ms: 240,
            ..final_usage
        })
        .await
        .expect("same final metrics are idempotent after restart");
    assert_eq!(duplicate_final.id, usage_id);
    assert!(!duplicate_final.applied);
    let conflict = reopened
        .finalize_ai_usage(FinalAiUsage {
            cost_micros: Some(31),
            ..final_usage
        })
        .await
        .expect_err("changed final metrics conflict");
    assert_eq!(conflict.code(), ErrorCode::UsageConflict);
    let rollback = reopened
        .start_ai_usage(start, 250)
        .await
        .expect_err("final usage cannot return to started");
    assert_eq!(rollback.code(), ErrorCode::UsageConflict);
    let pool = database.pool().await;
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM ai_usage")
        .fetch_one(&pool)
        .await
        .expect("count usage rows");
    assert_eq!(count, 1);
}
