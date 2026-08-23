use std::{fs, path::PathBuf};

use flori_core::{
    ArtifactId, DomainId, JobId, PipelineId, PipelineRevisionId, PromptSnapshotId, SourceId,
    TaskId, UploadId,
};
use flori_store::{
    Store,
    artifact::{NasArtifactStore, retained_artifact_path, task_artifact_path},
};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

struct Fixture {
    root: PathBuf,
    artifacts: PathBuf,
    store: Store,
    pool: SqlitePool,
    source: SourceId,
    revision: PipelineRevisionId,
}

impl Fixture {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-job-prune-{}", SourceId::generate()));
        fs::create_dir(&root).expect("fixture root");
        let database = root.join("flori.sqlite");
        let artifacts = root.join("artifacts");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let domain = DomainId::generate();
        let pipeline = PipelineId::generate();
        let revision = PipelineRevisionId::generate();
        let source = SourceId::generate();
        sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
            .bind(domain.to_string()).bind(format!("d-{domain}")).bind("domain").execute(&pool).await.expect("domain");
        sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
            .bind(pipeline.to_string())
            .bind(format!("p-{pipeline}"))
            .execute(&pool)
            .await
            .expect("pipeline");
        sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'tasks: {}',0)")
            .bind(revision.to_string()).bind(pipeline.to_string()).bind("a".repeat(64)).execute(&pool).await.expect("revision");
        sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload',?,?,?,?,0,0)")
            .bind(source.to_string()).bind(format!("upload:{source}")).bind(domain.to_string())
            .bind(format!("s-{source}")).bind("b".repeat(64)).execute(&pool).await.expect("source");
        Self {
            root,
            artifacts,
            store,
            pool,
            source,
            revision,
        }
    }

    async fn job(&self, state: &str, finished: i64) -> (JobId, TaskId) {
        let job = JobId::generate();
        let task = TaskId::generate();
        sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms,finished_at_ms) VALUES(?,?,?,'initial',?,?,?,'{}','{\"translate\":false}',?,?,0,0,?)")
            .bind(job.to_string()).bind(self.source.to_string()).bind(self.revision.to_string()).bind(state)
            .bind(PromptSnapshotId::generate().to_string()).bind("c".repeat(64)).bind(format!("j-{job}"))
            .bind("d".repeat(64)).bind(finished).execute(&self.pool).await.expect("job");
        sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms,started_at_ms,finished_at_ms) VALUES(?,?,'task','core.publish','{}','{}','succeeded',1,1000,0,?)")
            .bind(task.to_string()).bind(job.to_string()).bind(finished).execute(&self.pool).await.expect("task");
        (job, task)
    }

    async fn artifact(
        &self,
        job: JobId,
        task: TaskId,
        kind: &str,
        retention: &str,
    ) -> (ArtifactId, String) {
        let id = ArtifactId::generate();
        let path = if retention == "source" {
            retained_artifact_path(self.source, id, "item.json").expect("retained path")
        } else {
            task_artifact_path(self.source, job, task, id, "item.json").expect("task path")
        };
        let file = self.artifacts.join(&path);
        fs::create_dir_all(file.parent().expect("parent")).expect("artifact dirs");
        fs::write(&file, b"x").expect("artifact bytes");
        sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,'materialized',?,?, 'application/json','item.json',1,?,?,?,0)")
            .bind(id.to_string()).bind(self.source.to_string()).bind(job.to_string()).bind(task.to_string())
            .bind(format!("{kind}-{id}")).bind(kind).bind("e".repeat(64)).bind(&path).bind(retention)
            .execute(&self.pool).await.expect("artifact row");
        (id, path)
    }

    fn nas(&self) -> NasArtifactStore {
        NasArtifactStore::new(&self.artifacts, 1024).expect("NAS")
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).expect("cleanup");
    }
}

#[tokio::test]
async fn pruning_keeps_two_published_jobs_and_only_latest_failed_audit() {
    let fixture = Fixture::new().await;
    let current = fixture.job("succeeded", 6).await;
    let previous = fixture.job("succeeded", 5).await;
    let old = fixture.job("succeeded", 4).await;
    let failed_old = fixture.job("failed", 2).await;
    let failed_latest = fixture.job("failed", 3).await;
    sqlx::query("UPDATE sources SET current_job_id=?,previous_job_id=? WHERE id=?")
        .bind(current.0.to_string())
        .bind(previous.0.to_string())
        .bind(fixture.source.to_string())
        .execute(&fixture.pool)
        .await
        .expect("pointers");
    let current_path = fixture
        .artifact(current.0, current.1, "smart_note", "published")
        .await
        .1;
    let previous_path = fixture
        .artifact(previous.0, previous.1, "smart_note", "published")
        .await
        .1;
    let (old_artifact, old_path) = fixture
        .artifact(old.0, old.1, "smart_note", "published")
        .await;
    let retained = fixture
        .artifact(old.0, old.1, "source_original", "source")
        .await
        .1;
    let old_log = fixture
        .artifact(failed_old.0, failed_old.1, "task_log", "failed_audit")
        .await
        .1;
    let latest_log = fixture
        .artifact(failed_latest.0, failed_latest.1, "task_log", "failed_audit")
        .await
        .1;
    let latest_audit = fixture
        .artifact(failed_latest.0, failed_latest.1, "ai_audit", "failed_audit")
        .await
        .1;
    let latest_note = fixture
        .artifact(failed_latest.0, failed_latest.1, "smart_note", "published")
        .await
        .1;
    let upload = UploadId::generate();
    sqlx::query("INSERT INTO uploads(id,owner_kind,owner_id,request_sha256,commit_json,name,target_id,source_artifact_id,staging_path,final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) VALUES(?,'materialize',?,?,'{}','copy',?,?,?, ?,1,?,0,'receiving',0,0)")
        .bind(upload.to_string()).bind(JobId::generate().to_string()).bind("f".repeat(64)).bind(ArtifactId::generate().to_string())
        .bind(old_artifact.to_string()).bind(format!(".staging/{upload}" )).bind(&old_path).bind("e".repeat(64))
        .execute(&fixture.pool).await.expect("materialize ledger");
    fixture
        .store
        .prune_source_jobs(&fixture.nas(), fixture.source)
        .await
        .expect("first prune");
    assert!(fixture.artifacts.join(&old_path).is_file());
    sqlx::query("DELETE FROM uploads WHERE id=?")
        .bind(upload.to_string())
        .execute(&fixture.pool)
        .await
        .expect("ledger done");
    fixture
        .store
        .prune_source_jobs(&fixture.nas(), fixture.source)
        .await
        .expect("prune");
    for path in [
        &current_path,
        &previous_path,
        &retained,
        &latest_log,
        &latest_audit,
    ] {
        assert!(fixture.artifacts.join(path).is_file(), "kept {path}");
    }
    for path in [&old_path, &old_log, &latest_note] {
        assert!(!fixture.artifacts.join(path).exists(), "removed {path}");
    }
    let jobs: i64 = sqlx::query_scalar("SELECT count(*) FROM jobs WHERE source_id=?")
        .bind(fixture.source.to_string())
        .fetch_one(&fixture.pool)
        .await
        .expect("jobs");
    assert_eq!(jobs, 5);
}

#[tokio::test]
async fn job_trash_recovery_restores_before_commit_and_finishes_after_commit() {
    let fixture = Fixture::new().await;
    let canceled = fixture.job("canceled", 1).await;
    let (_, path) = fixture
        .artifact(canceled.0, canceled.1, "task_log", "failed_audit")
        .await;
    let job_dir = fixture
        .artifacts
        .join(format!("sources/{}/jobs/{}", fixture.source, canceled.0));
    let trash = fixture
        .artifacts
        .join(format!(".trash/jobs/{}", canceled.0));
    fs::create_dir_all(trash.parent().expect("trash parent")).expect("trash root");
    fs::rename(&job_dir, &trash).expect("crash before commit");
    fixture
        .store
        .reconcile_job_prunes(&fixture.nas())
        .await
        .expect("restore");
    assert!(fixture.artifacts.join(&path).is_file());
    fs::rename(&job_dir, &trash).expect("crash after commit");
    sqlx::query("DELETE FROM artifacts WHERE job_id=?")
        .bind(canceled.0.to_string())
        .execute(&fixture.pool)
        .await
        .expect("commit rows");
    fixture
        .store
        .reconcile_job_prunes(&fixture.nas())
        .await
        .expect("finish");
    assert!(!trash.exists());
}
