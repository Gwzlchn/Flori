use std::fs;

use flori_core::{
    DomainId, ErrorCode, JobId, JobState, JobStateEvent, PipelineId, PipelineRevisionId,
    PromptSnapshotId, SourceChangedEvent, SourceId, SystemHealthStatus,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

#[tokio::test]
async fn event_feed_is_strict_resumable_and_reports_real_health() {
    let root = std::env::temp_dir().join(format!("flori-events-{}", SourceId::generate()));
    fs::create_dir(&root).expect("fixture root");
    let database = root.join("flori.sqlite");
    let artifacts = NasArtifactStore::new(root.join("artifacts"), 1024).expect("NAS");
    let store = Store::open(&database).await.expect("store");
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(database)
            .foreign_keys(true),
    )
    .await
    .expect("pool");
    let (source, job) = seed(&pool).await;

    let first = insert_event(
        &pool,
        "job",
        Some(job.to_string()),
        "job_state",
        serde_json::to_string(&JobStateEvent {
            job_id: job,
            state: JobState::Succeeded,
            error_code: None,
        })
        .expect("job event JSON"),
        1,
    )
    .await;
    insert_event(
        &pool,
        "source",
        Some(source.to_string()),
        "source_changed",
        serde_json::to_string(&SourceChangedEvent { source_id: source })
            .expect("source event JSON"),
        2,
    )
    .await;
    store
        .record_system_health(&artifacts, 0, 3)
        .await
        .expect("health event");
    assert_eq!(
        store.read_events(None, 0, 10).await.expect("global").len(),
        3
    );
    let job_events = store
        .read_events(Some(job), 0, 10)
        .await
        .expect("job events");
    assert_eq!(job_events.len(), 1);
    assert_eq!(job_events[0].id, first);
    assert_eq!(
        store
            .read_events(Some(job), 2, 10)
            .await
            .expect_err("cursor from another feed")
            .code(),
        ErrorCode::EventCursorExpired
    );

    sqlx::query("DELETE FROM job_events WHERE id=2")
        .execute(&pool)
        .await
        .expect("expire non-prefix cursor fixture");
    assert_eq!(
        store
            .read_events(None, 2, 10)
            .await
            .expect_err("expired non-prefix cursor")
            .code(),
        ErrorCode::EventCursorExpired
    );
    sqlx::query("DELETE FROM job_events WHERE id=1")
        .execute(&pool)
        .await
        .expect("expire cursor fixture");
    assert_eq!(
        store
            .read_events(None, 1, 10)
            .await
            .expect_err("expired")
            .code(),
        ErrorCode::EventCursorExpired
    );
    sqlx::query("INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) VALUES('system',NULL,'job_state','{\"job_id\":\"bad\"}',4)")
        .execute(&pool).await.expect("corrupt event");
    assert_eq!(
        store
            .read_events(None, 3, 10)
            .await
            .expect_err("corrupt")
            .code(),
        ErrorCode::CorruptState
    );
    let system = store.system_view(&artifacts, 0).await.expect("system view");
    assert_eq!(system.status, SystemHealthStatus::Healthy);
    assert_eq!(system.queue_depth, 0);
    assert!(system.disk_free_bytes > 0);
    assert_eq!(
        (
            system.runners_total,
            system.usage_started,
            system.usage_final
        ),
        (0, 0, 0)
    );
    fs::remove_dir_all(root).expect("cleanup");
}

async fn insert_event(
    pool: &SqlitePool,
    scope: &str,
    scope_id: Option<String>,
    kind: &str,
    payload_json: String,
    now_ms: i64,
) -> u64 {
    sqlx::query(
        "INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
    )
    .bind(scope)
    .bind(scope_id)
    .bind(kind)
    .bind(payload_json)
    .bind(now_ms)
    .execute(pool)
    .await
    .expect("event fixture")
    .last_insert_rowid()
    .try_into()
    .expect("event ID")
}

async fn seed(pool: &SqlitePool) -> (SourceId, JobId) {
    let domain = DomainId::generate();
    let pipeline = PipelineId::generate();
    let revision = PipelineRevisionId::generate();
    let source = SourceId::generate();
    let job = JobId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
        .bind(domain.to_string()).bind(format!("d-{domain}")).bind("domain").execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
        .bind(pipeline.to_string())
        .bind(format!("p-{pipeline}"))
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'tasks: {}',0)")
        .bind(revision.to_string()).bind(pipeline.to_string()).bind("a".repeat(64)).execute(pool).await.expect("revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload',?,?,?,?,0,0)")
        .bind(source.to_string()).bind(format!("upload:{source}")).bind(domain.to_string())
        .bind(format!("s-{source}")).bind("b".repeat(64)).execute(pool).await.expect("source");
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms,finished_at_ms) VALUES(?,?,?,'initial','succeeded',?,?,'{}','{\"translate\":false}',?,?,0,0,1)")
        .bind(job.to_string()).bind(source.to_string()).bind(revision.to_string())
        .bind(PromptSnapshotId::generate().to_string()).bind("c".repeat(64))
        .bind(format!("j-{job}")).bind("d".repeat(64)).execute(pool).await.expect("job");
    sqlx::query("UPDATE sources SET current_job_id=? WHERE id=?")
        .bind(job.to_string())
        .bind(source.to_string())
        .execute(pool)
        .await
        .expect("current");
    (source, job)
}
