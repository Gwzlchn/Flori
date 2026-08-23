use std::{fs, path::PathBuf};

use flori_core::{
    AttemptId, DomainId, ErrorCode, JobId, PipelineId, PipelineRevisionId, PromptSnapshotId,
    RunnerId, SourceId, SourceInputId, TaskId, UploadId,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

struct Fixture {
    root: PathBuf,
    artifacts: PathBuf,
}

impl Fixture {
    async fn new() -> (Self, Store, SqlitePool, NasArtifactStore) {
        let root = std::env::temp_dir().join(format!("flori-lifecycle-{}", SourceId::generate()));
        fs::create_dir(&root).expect("fixture root");
        let database = root.join("flori.sqlite");
        let artifacts = root.join("artifacts");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let nas = NasArtifactStore::new(&artifacts, 1024 * 1024).expect("NAS");
        (Self { root, artifacts }, store, pool, nas)
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).expect("remove fixture");
    }
}

struct Seed {
    source: SourceId,
    job: JobId,
}

async fn seed(pool: &SqlitePool, job_state: &str, task_state: &str) -> Seed {
    let domain = DomainId::generate();
    let pipeline = PipelineId::generate();
    let revision = PipelineRevisionId::generate();
    let source = SourceId::generate();
    let job = JobId::generate();
    let task = TaskId::generate();
    let attempt = AttemptId::generate();
    let runner = RunnerId::generate();
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
    sqlx::query("UPDATE pipelines SET current_revision_id=? WHERE id=?")
        .bind(revision.to_string())
        .bind(pipeline.to_string())
        .execute(pool)
        .await
        .expect("current revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload',?,?,?,?,0,0)")
        .bind(source.to_string()).bind(format!("upload:{source}")).bind(domain.to_string())
        .bind(format!("s-{source}")).bind("b".repeat(64)).execute(pool).await.expect("source");
    sqlx::query("INSERT INTO runners(id,name,state,config_revision,max_concurrency,tags_json,tools_json,ai_models_json,created_at_ms,updated_at_ms) VALUES(?,?,'enabled',1,1,'[]','[]','[]',0,0)")
        .bind(runner.to_string()).bind(format!("r-{runner}")).execute(pool).await.expect("runner");
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms) VALUES(?,?,?,'initial',?,?,?,?,? ,?,?,0,0)")
        .bind(job.to_string()).bind(source.to_string()).bind(revision.to_string()).bind(job_state)
        .bind(PromptSnapshotId::generate().to_string()).bind("c".repeat(64)).bind("{}")
        .bind(r#"{"translate":false}"#).bind(format!("j-{job}")).bind("d".repeat(64))
        .execute(pool).await.expect("job");
    sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms,started_at_ms) VALUES(?,?,'note','ai.document_note',?,'{}','ready',1,60000,0)")
        .bind(task.to_string()).bind(job.to_string())
        .bind(r#"{"executor":"ai.document_note","needs":[],"tags":[],"retry":0,"timeout_ms":60000,"artifacts":[]}"#)
        .execute(pool).await.expect("task");
    sqlx::query("INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,model,effort,runner_config_revision,lease_expires_at_ms,last_log_sequence,started_at_ms) VALUES(?,?,1,?,'leased','Ultimate','high',1,1000,0,0)")
        .bind(attempt.to_string()).bind(task.to_string()).bind(runner.to_string()).execute(pool).await.expect("attempt");
    sqlx::query("UPDATE tasks SET state=?,current_attempt_id=? WHERE id=?")
        .bind(task_state)
        .bind(attempt.to_string())
        .bind(task.to_string())
        .execute(pool)
        .await
        .expect("lease task");
    Seed { source, job }
}

#[tokio::test]
async fn delete_source_is_atomic_idempotent_and_recovers_both_crash_sides() {
    let (fixture, store, pool, artifacts) = Fixture::new().await;
    let active = seed(&pool, "running", "leased").await;
    assert_eq!(
        store
            .delete_source(&artifacts, active.source)
            .await
            .expect_err("active source")
            .code(),
        ErrorCode::SourceBusy
    );
    sqlx::query("UPDATE attempts SET state='canceled',finished_at_ms=2 WHERE task_id IN (SELECT id FROM tasks WHERE job_id=?)")
        .bind(active.job.to_string()).execute(&pool).await.expect("cancel attempt");
    sqlx::query("UPDATE tasks SET state='canceled',finished_at_ms=2 WHERE job_id=?")
        .bind(active.job.to_string())
        .execute(&pool)
        .await
        .expect("cancel task");
    sqlx::query("UPDATE jobs SET state='canceled',finished_at_ms=2 WHERE id=?")
        .bind(active.job.to_string())
        .execute(&pool)
        .await
        .expect("cancel job");
    let input = SourceInputId::generate();
    sqlx::query("INSERT INTO source_inputs(id,source_id,name,media_type,size_bytes,sha256,relative_path,created_at_ms) VALUES(?,?,'original','application/pdf',1,? ,?,0)")
        .bind(input.to_string()).bind(active.source.to_string()).bind("e".repeat(64))
        .bind(format!("sources/{}/inputs/{input}/source.pdf", active.source)).execute(&pool).await.expect("input");
    let source_root = fixture
        .artifacts
        .join("sources")
        .join(active.source.to_string());
    fs::create_dir_all(source_root.join("inputs").join(input.to_string())).expect("input dir");
    fs::write(
        source_root
            .join("inputs")
            .join(input.to_string())
            .join("source.pdf"),
        b"x",
    )
    .expect("input bytes");
    let upload = UploadId::generate();
    sqlx::query("INSERT INTO uploads(id,owner_kind,owner_id,request_key,request_sha256,commit_json,name,target_id,staging_path,final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) VALUES(?,'source',?,?,?,'{}','original',?,? ,?,1,?,0,'receiving',0,0)")
        .bind(upload.to_string()).bind(active.source.to_string()).bind(format!("u-{upload}"))
        .bind("f".repeat(64)).bind(input.to_string()).bind(format!(".staging/{upload}.part"))
        .bind(format!("sources/{}/inputs/{input}/source.pdf", active.source))
        .bind("e".repeat(64)).execute(&pool).await.expect("upload ledger");
    assert_eq!(
        store
            .delete_source(&artifacts, active.source)
            .await
            .expect_err("open upload")
            .code(),
        ErrorCode::SourceBusy
    );
    sqlx::query("DELETE FROM uploads WHERE id=?")
        .bind(upload.to_string())
        .execute(&pool)
        .await
        .expect("remove upload ledger");
    sqlx::query("UPDATE source_inputs SET relative_path=? WHERE id=?")
        .bind(format!(
            "sources/{}/inputs/{input}/source.pdf",
            SourceId::generate()
        ))
        .bind(input.to_string())
        .execute(&pool)
        .await
        .expect("drift path");
    assert_eq!(
        store
            .delete_source(&artifacts, active.source)
            .await
            .expect_err("path drift")
            .code(),
        ErrorCode::CorruptState
    );
    sqlx::query("UPDATE source_inputs SET relative_path=? WHERE id=?")
        .bind(format!(
            "sources/{}/inputs/{input}/source.pdf",
            active.source
        ))
        .bind(input.to_string())
        .execute(&pool)
        .await
        .expect("restore path");
    let outside = fixture.root.join("outside");
    fs::write(&outside, b"outside").expect("outside file");
    std::os::unix::fs::symlink(&outside, source_root.join("unsafe-link")).expect("symlink");
    assert_eq!(
        store
            .delete_source(&artifacts, active.source)
            .await
            .expect_err("symlink")
            .code(),
        ErrorCode::ArtifactInvalidPath
    );
    fs::remove_file(source_root.join("unsafe-link")).expect("remove symlink");
    store
        .delete_source(&artifacts, active.source)
        .await
        .expect("delete");
    store
        .delete_source(&artifacts, active.source)
        .await
        .expect("repeat delete");
    let remaining: i64 = sqlx::query_scalar("SELECT (SELECT count(*) FROM sources)+(SELECT count(*) FROM jobs)+(SELECT count(*) FROM tasks)+(SELECT count(*) FROM attempts)+(SELECT count(*) FROM source_inputs)+(SELECT count(*) FROM ai_usage)")
        .fetch_one(&pool).await.expect("remaining");
    assert_eq!(remaining, 0);
    assert!(!source_root.exists());
    assert!(
        !fixture
            .artifacts
            .join(".trash/sources")
            .join(active.source.to_string())
            .exists()
    );

    let restored = seed(&pool, "running", "leased").await;
    sqlx::query("UPDATE jobs SET state='canceled' WHERE id=?")
        .bind(restored.job.to_string())
        .execute(&pool)
        .await
        .expect("terminal");
    let restored_root = fixture
        .artifacts
        .join("sources")
        .join(restored.source.to_string());
    fs::create_dir_all(&restored_root).expect("source dir");
    let trash = fixture.artifacts.join(".trash/sources");
    fs::create_dir_all(&trash).expect("trash root");
    fs::rename(&restored_root, trash.join(restored.source.to_string())).expect("crash before DB");
    assert_eq!(
        store
            .reconcile_source_deletes(&artifacts)
            .await
            .expect_err("restore reports incomplete")
            .code(),
        ErrorCode::Conflict
    );
    assert!(restored_root.is_dir());

    fs::rename(&restored_root, trash.join(restored.source.to_string())).expect("crash after DB");
    sqlx::query("DELETE FROM sources WHERE id=?")
        .bind(restored.source.to_string())
        .execute(&pool)
        .await
        .expect("delete row");
    store
        .reconcile_source_deletes(&artifacts)
        .await
        .expect("finish delete");
    assert!(!trash.join(restored.source.to_string()).exists());
}
