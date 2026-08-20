use std::{fmt::Write, fs, path::PathBuf};

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactManifestEntry, ArtifactRetention,
    ArtifactWhen, AttemptId, CompiledTaskSpec, DomainId, ErrorCode, Executor, JobId, JobInputs,
    LogFrame, PendingAttemptUpload, PendingMaterializeCommit, PendingMaterializedArtifact,
    PendingTaskCommit, PipelineId, PipelineRevisionId, PromptSnapshot, PromptSnapshotId,
    PromptSnapshotProfile, RunnerId, Sha256Digest, SourceId, TaskId, TaskInputBindings,
    TaskInputReference, TaskLogEvent, TaskLogLevel, TaskLogLine, TaskState, UploadId, UploadState,
};
use flori_store::{
    Store,
    artifact::{NasArtifactStore, UploadRecord, task_artifact_path},
};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};

struct Fixture {
    directory: PathBuf,
    artifact_root: PathBuf,
    store: Store,
    pool: SqlitePool,
    revision_id: PipelineRevisionId,
    source_id: SourceId,
    job_id: JobId,
    task_id: TaskId,
    attempt_id: AttemptId,
}

impl Fixture {
    async fn active(declarations: Vec<ArtifactDeclaration>) -> Self {
        let directory = std::env::temp_dir().join(format!(
            "flori-upload-recovery-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&directory).expect("create test directory");
        let database = directory.join("flori.sqlite");
        let artifact_root = directory.join("artifacts");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("test pool");
        let domain_id = DomainId::generate();
        let pipeline_id = PipelineId::generate();
        let revision_id = PipelineRevisionId::generate();
        let source_id = SourceId::generate();
        let job_id = JobId::generate();
        let task_id = TaskId::generate();
        let attempt_id = AttemptId::generate();
        let runner_id = RunnerId::generate();
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
        sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
            .bind(pipeline_id.to_string())
            .bind(format!("pipeline-{pipeline_id}"))
            .execute(&pool)
            .await
            .expect("pipeline");
        sqlx::query(
            "INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit, \
             yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'test: {}',0)",
        )
        .bind(revision_id.to_string())
        .bind(pipeline_id.to_string())
        .bind("1".repeat(64))
        .execute(&pool)
        .await
        .expect("revision");
        sqlx::query("UPDATE pipelines SET current_revision_id=? WHERE id=?")
            .bind(revision_id.to_string())
            .bind(pipeline_id.to_string())
            .execute(&pool)
            .await
            .expect("current revision");
        sqlx::query(
            "INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256, \
             created_at_ms,updated_at_ms) VALUES(?,'pdf_url',?,?,?,?,0,0)",
        )
        .bind(source_id.to_string())
        .bind(format!("https://example.test/{source_id}.pdf"))
        .bind(domain_id.to_string())
        .bind(format!("source-{source_id}"))
        .bind("2".repeat(64))
        .execute(&pool)
        .await
        .expect("source");
        sqlx::query(
            "INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id, \
             prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256, \
             created_at_ms,started_at_ms) VALUES(?,?,?,'initial','running',?,?,'{}', \
             '{\"translate\":false}',?,?,0,0)",
        )
        .bind(job_id.to_string())
        .bind(source_id.to_string())
        .bind(revision_id.to_string())
        .bind(PromptSnapshotId::generate().to_string())
        .bind("3".repeat(64))
        .bind(format!("job-{job_id}"))
        .bind("4".repeat(64))
        .execute(&pool)
        .await
        .expect("job");
        sqlx::query(
            "INSERT INTO runners(id,name,state,config_revision,max_concurrency,tags_json, \
             tools_json,ai_models_json,created_at_ms,updated_at_ms) \
             VALUES(?,?,'enabled',1,1,'[]','[]','[]',0,0)",
        )
        .bind(runner_id.to_string())
        .bind(format!("runner-{runner_id}"))
        .execute(&pool)
        .await
        .expect("runner");
        let spec = CompiledTaskSpec {
            executor: Executor::DocumentAcquire,
            needs: Vec::new(),
            tags: Vec::new(),
            retry: 0,
            timeout_ms: 1_000,
            artifacts: declarations,
        };
        sqlx::query(
            "INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state, \
             attempt_limit,timeout_ms,ready_at_ms,started_at_ms) \
             VALUES(?,?,'work','document.acquire',?,?,'leased',1,1000,0,0)",
        )
        .bind(task_id.to_string())
        .bind(job_id.to_string())
        .bind(serde_json::to_string(&spec).expect("spec"))
        .bind(bindings_json())
        .execute(&pool)
        .await
        .expect("task");
        sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms) VALUES(?,?,1,?,'leased',1000,0,0)",
        )
        .bind(attempt_id.to_string())
        .bind(task_id.to_string())
        .bind(runner_id.to_string())
        .execute(&pool)
        .await
        .expect("attempt");
        sqlx::query("UPDATE tasks SET current_attempt_id=? WHERE id=?")
            .bind(attempt_id.to_string())
            .bind(task_id.to_string())
            .execute(&pool)
            .await
            .expect("current attempt");
        Self {
            directory,
            artifact_root,
            store,
            pool,
            revision_id,
            source_id,
            job_id,
            task_id,
            attempt_id,
        }
    }

    fn artifacts(&self) -> NasArtifactStore {
        NasArtifactStore::new(&self.artifact_root, 1024 * 1024).expect("artifact store")
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove test directory");
    }
}

#[tokio::test]
async fn attempt_recovery_accepts_file_ahead_and_closes_verified_rename_window() {
    let fixture = Fixture::active(vec![note_declaration()]).await;
    let artifacts = fixture.artifacts();
    let bytes = b"# note";
    let upload =
        insert_attempt_upload(&fixture, &artifacts, bytes, UploadState::Receiving, 0, true).await;

    fixture
        .store
        .reconcile_uploads(&artifacts, 10)
        .await
        .expect("file-ahead remains resumable");
    assert_upload(&fixture.pool, upload.id, "receiving", 0).await;
    sqlx::query("UPDATE uploads SET received_bytes=?,state='verified' WHERE id=?")
        .bind(i64::try_from(bytes.len()).expect("size"))
        .bind(upload.id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("simulate verified commit");
    fixture
        .store
        .reconcile_uploads(&artifacts, 11)
        .await
        .expect("rename and mark moved");
    assert_upload(&fixture.pool, upload.id, "moved", bytes.len() as i64).await;
    assert_eq!(
        fs::read(fixture.artifact_root.join(&upload.final_path)).expect("final artifact"),
        bytes
    );
    fixture
        .store
        .reconcile_uploads(&artifacts, 12)
        .await
        .expect("moved ledger is stable");
    sqlx::query("UPDATE uploads SET state='verified' WHERE id=?")
        .bind(upload.id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("simulate rename before state commit");
    fixture
        .store
        .reconcile_uploads(&artifacts, 13)
        .await
        .expect("final-only verified ledger converges to moved");
    assert_upload(&fixture.pool, upload.id, "moved", bytes.len() as i64).await;
}

#[tokio::test]
async fn expired_attempt_is_discarded_but_corrupt_digest_is_fail_closed() {
    let fixture = Fixture::active(vec![note_declaration()]).await;
    let artifacts = fixture.artifacts();
    let bytes = b"# note";
    let upload = insert_attempt_upload(
        &fixture,
        &artifacts,
        bytes,
        UploadState::Moved,
        bytes.len(),
        false,
    )
    .await;
    write_final(&fixture, &upload.final_path, bytes);
    fixture
        .store
        .reconcile_uploads(&artifacts, 1_000)
        .await
        .expect("expired owner cleanup");
    assert_eq!(upload_count(&fixture.pool).await, 0);
    assert!(!fixture.artifact_root.join(&upload.final_path).exists());

    let fixture = Fixture::active(vec![note_declaration()]).await;
    let artifacts = fixture.artifacts();
    let upload = insert_attempt_upload_with_digest(
        &fixture,
        &artifacts,
        bytes,
        digest(b"different"),
        UploadState::Verified,
        bytes.len(),
        true,
    )
    .await;
    assert_eq!(
        fixture
            .store
            .reconcile_uploads(&artifacts, 10)
            .await
            .expect_err("digest mismatch")
            .code(),
        ErrorCode::CorruptState
    );
    assert_eq!(upload_count(&fixture.pool).await, 1);
    assert!(
        fixture
            .artifact_root
            .join(format!(".staging/uploads/{}", upload.id))
            .exists()
    );
}

#[tokio::test]
async fn server_log_final_file_rebuilds_the_strict_pending_commit() {
    let fixture = Fixture::active(vec![log_declaration()]).await;
    let artifacts = fixture.artifacts();
    let line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: 7,
        level: TaskLogLevel::Info,
        message: "done".into(),
    })
    .expect("log line");
    let bytes = format!("{line}\n").into_bytes();
    let upload_id = UploadId::generate();
    let artifact_id = ArtifactId::generate();
    let final_path = task_artifact_path(
        fixture.source_id,
        fixture.job_id,
        fixture.task_id,
        artifact_id,
        "task.ndjson",
    )
    .expect("log path");
    sqlx::query(
        "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
         final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state, \
         created_at_ms,updated_at_ms) VALUES(?,'attempt',?,NULL,'task_log',?,?,?,?,?,?, \
         'receiving',0,0)",
    )
    .bind(upload_id.to_string())
    .bind(fixture.attempt_id.to_string())
    .bind(artifact_id.to_string())
    .bind(format!(".staging/uploads/{upload_id}"))
    .bind(&final_path)
    .bind(1024_i64)
    .bind(digest(&bytes).as_str())
    .bind(i64::try_from(bytes.len()).expect("log size"))
    .execute(&fixture.pool)
    .await
    .expect("log ledger");
    let event = TaskLogEvent {
        exec_id: fixture.attempt_id,
        frame: LogFrame {
            sequence: 1,
            sha256: digest(line.as_bytes()),
            line,
        },
    };
    sqlx::query(
        "INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) \
         VALUES('job',?,'log_cursor',?,1)",
    )
    .bind(fixture.job_id.to_string())
    .bind(serde_json::to_string(&event).expect("event"))
    .execute(&fixture.pool)
    .await
    .expect("event");
    sqlx::query("UPDATE attempts SET last_log_sequence=1 WHERE id=?")
        .bind(fixture.attempt_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("cursor");
    write_final(&fixture, &final_path, &bytes);

    fixture
        .store
        .reconcile_uploads(&artifacts, 10)
        .await
        .expect("recover server log rename window");
    let row = sqlx::query("SELECT commit_json,state,expected_size_bytes FROM uploads WHERE id=?")
        .bind(upload_id.to_string())
        .fetch_one(&fixture.pool)
        .await
        .expect("recovered ledger");
    let pending: PendingAttemptUpload =
        serde_json::from_str(row.try_get("commit_json").expect("commit json"))
            .expect("strict pending upload");
    assert_eq!(pending.artifact.kind, ArtifactKind::TaskLog);
    assert_eq!(pending.artifact.size_bytes, bytes.len() as u64);
    assert_eq!(row.try_get::<String, _>("state").expect("state"), "moved");
}

#[tokio::test]
async fn materialize_moved_is_kept_then_source_drift_discards_it() {
    let fixture = Fixture::active(vec![note_declaration()]).await;
    let artifacts = fixture.artifacts();
    let pending = seed_materialize(&fixture, b"# old note").await;
    fixture
        .store
        .reconcile_uploads(&artifacts, 10)
        .await
        .expect("valid moved materialization waits for request retry");
    assert_eq!(upload_count(&fixture.pool).await, 1);

    let replacement = JobId::generate();
    insert_succeeded_job(&fixture, replacement, "replacement").await;
    sqlx::query("UPDATE sources SET current_job_id=? WHERE id=?")
        .bind(replacement.to_string())
        .bind(fixture.source_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("drift current source");
    fixture
        .store
        .reconcile_uploads(&artifacts, 11)
        .await
        .expect("invalid materialize cleanup");
    assert_eq!(upload_count(&fixture.pool).await, 0);
    assert!(
        !fixture
            .artifact_root
            .join(&pending.artifacts[0].final_relative_path)
            .exists()
    );
}

struct TestUpload {
    id: UploadId,
    final_path: String,
}

async fn insert_attempt_upload(
    fixture: &Fixture,
    artifacts: &NasArtifactStore,
    bytes: &[u8],
    state: UploadState,
    received: usize,
    staging: bool,
) -> TestUpload {
    insert_attempt_upload_with_digest(
        fixture,
        artifacts,
        bytes,
        digest(bytes),
        state,
        received,
        staging,
    )
    .await
}

async fn insert_attempt_upload_with_digest(
    fixture: &Fixture,
    artifacts: &NasArtifactStore,
    bytes: &[u8],
    expected_sha256: Sha256Digest,
    state: UploadState,
    received: usize,
    staging: bool,
) -> TestUpload {
    let upload_id = UploadId::generate();
    let artifact_id = ArtifactId::generate();
    let final_path = task_artifact_path(
        fixture.source_id,
        fixture.job_id,
        fixture.task_id,
        artifact_id,
        "note.md",
    )
    .expect("artifact path");
    let pending = PendingAttemptUpload {
        artifact_id,
        declaration_name: "smart_note".into(),
        artifact: ArtifactManifestEntry {
            name: "smart_note".into(),
            kind: ArtifactKind::SmartNote,
            media_type: "text/markdown".into(),
            size_bytes: bytes.len() as u64,
            sha256: expected_sha256.clone(),
            relative_path: final_path.clone(),
        },
    };
    sqlx::query(
        "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
         final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state, \
         created_at_ms,updated_at_ms) VALUES(?,'attempt',?,?,'smart_note',?,?,?,?,?,?,?,0,0)",
    )
    .bind(upload_id.to_string())
    .bind(fixture.attempt_id.to_string())
    .bind(serde_json::to_string(&pending).expect("pending upload"))
    .bind(artifact_id.to_string())
    .bind(format!(".staging/uploads/{upload_id}"))
    .bind(&final_path)
    .bind(i64::try_from(bytes.len()).expect("size"))
    .bind(expected_sha256.as_str())
    .bind(i64::try_from(received).expect("received"))
    .bind(wire_state(state))
    .execute(&fixture.pool)
    .await
    .expect("upload ledger");
    if staging {
        let record = UploadRecord::new(
            upload_id,
            "smart_note",
            &final_path,
            bytes.len() as u64,
            expected_sha256,
            "smart_note",
            1024,
        )
        .expect("upload record");
        artifacts
            .append_chunk(&record, 0, &digest(bytes), bytes)
            .expect("write staging");
    }
    TestUpload {
        id: upload_id,
        final_path,
    }
}

async fn seed_materialize(fixture: &Fixture, bytes: &[u8]) -> PendingMaterializeCommit {
    sqlx::query("UPDATE attempts SET state='succeeded',finished_at_ms=1 WHERE id=?")
        .bind(fixture.attempt_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("succeed attempt");
    sqlx::query("UPDATE tasks SET state='succeeded',finished_at_ms=1 WHERE id=?")
        .bind(fixture.task_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("succeed task");
    sqlx::query("UPDATE jobs SET state='succeeded',finished_at_ms=1 WHERE id=?")
        .bind(fixture.job_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("succeed job");
    sqlx::query("UPDATE sources SET current_job_id=? WHERE id=?")
        .bind(fixture.job_id.to_string())
        .bind(fixture.source_id.to_string())
        .execute(&fixture.pool)
        .await
        .expect("publish base job");
    let source_artifact_id = ArtifactId::generate();
    let source_path = task_artifact_path(
        fixture.source_id,
        fixture.job_id,
        fixture.task_id,
        source_artifact_id,
        "note.md",
    )
    .expect("source path");
    write_final(fixture, &source_path, bytes);
    sqlx::query(
        "INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind, \
         media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) \
         VALUES(?,?,?,?,?,'produced','smart_note','smart_note','text/markdown','note.md', \
         ?,?,?,'published',1)",
    )
    .bind(source_artifact_id.to_string())
    .bind(fixture.source_id.to_string())
    .bind(fixture.job_id.to_string())
    .bind(fixture.task_id.to_string())
    .bind(fixture.attempt_id.to_string())
    .bind(i64::try_from(bytes.len()).expect("size"))
    .bind(digest(bytes).as_str())
    .bind(&source_path)
    .execute(&fixture.pool)
    .await
    .expect("source artifact");

    let job_id = JobId::generate();
    let task_id = TaskId::generate();
    let artifact_id = ArtifactId::generate();
    let upload_id = UploadId::generate();
    let final_path = task_artifact_path(fixture.source_id, job_id, task_id, artifact_id, "note.md")
        .expect("target path");
    let spec = CompiledTaskSpec {
        executor: Executor::DocumentAcquire,
        needs: Vec::new(),
        tags: Vec::new(),
        retry: 0,
        timeout_ms: 1_000,
        artifacts: vec![note_declaration()],
    };
    let pending = PendingMaterializeCommit {
        source_id: fixture.source_id,
        base_job_id: fixture.job_id,
        job_id,
        pipeline_revision_id: fixture.revision_id,
        prompt_snapshot_id: PromptSnapshotId::generate(),
        prompt_snapshot: PromptSnapshot {
            profile: PromptSnapshotProfile {
                domain_id: domain_id(&fixture.pool, fixture.source_id).await,
                profile_text: "profile".into(),
                sha256: digest(b"profile"),
            },
            prompts: Vec::new(),
        },
        inputs: JobInputs { translate: false },
        from_task_key: "later".into(),
        created_at_ms: 2,
        tasks: vec![PendingTaskCommit {
            task_id,
            task_key: "work".into(),
            spec,
            bindings: TaskInputBindings::DocumentAcquire {
                source: TaskInputReference::Source,
            },
            state: TaskState::Skipped,
            ai_selection: None,
        }],
        artifacts: vec![PendingMaterializedArtifact {
            upload_id,
            artifact_id,
            source_artifact_id,
            task_id,
            name: "smart_note".into(),
            kind: ArtifactKind::SmartNote,
            media_type: "text/markdown".into(),
            file_name: "note.md".into(),
            size_bytes: bytes.len() as u64,
            sha256: digest(bytes),
            retention: ArtifactRetention::Published,
            final_relative_path: final_path.clone(),
        }],
    };
    let commit = serde_json::to_string(&pending).expect("materialize commit");
    sqlx::query(
        "INSERT INTO uploads(id,owner_kind,owner_id,request_key,request_sha256,commit_json,name, \
         target_id,source_artifact_id,staging_path,final_relative_path,expected_size_bytes, \
         expected_sha256,received_bytes,state,created_at_ms,updated_at_ms) \
         VALUES(?,'materialize',?,'rerun-request',?,?,'work/smart_note',?,?,?,?,?,?,?,'moved',2,2)",
    )
    .bind(upload_id.to_string())
    .bind(job_id.to_string())
    .bind("5".repeat(64))
    .bind(commit)
    .bind(artifact_id.to_string())
    .bind(source_artifact_id.to_string())
    .bind(format!(".staging/uploads/{upload_id}"))
    .bind(&final_path)
    .bind(i64::try_from(bytes.len()).expect("size"))
    .bind(digest(bytes).as_str())
    .bind(i64::try_from(bytes.len()).expect("received"))
    .execute(&fixture.pool)
    .await
    .expect("materialize ledger");
    write_final(fixture, &final_path, bytes);
    pending
}

async fn insert_succeeded_job(fixture: &Fixture, job_id: JobId, suffix: &str) {
    sqlx::query(
        "INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id, \
         prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256, \
         created_at_ms,finished_at_ms) VALUES(?,?,?,'pipeline_rerun','succeeded',?,?,'{}', \
         '{\"translate\":false}',?,?,3,3)",
    )
    .bind(job_id.to_string())
    .bind(fixture.source_id.to_string())
    .bind(fixture.revision_id.to_string())
    .bind(PromptSnapshotId::generate().to_string())
    .bind("6".repeat(64))
    .bind(format!("job-{suffix}-{job_id}"))
    .bind("7".repeat(64))
    .execute(&fixture.pool)
    .await
    .expect("replacement job");
}

async fn domain_id(pool: &SqlitePool, source_id: SourceId) -> DomainId {
    let value: String = sqlx::query_scalar("SELECT domain_id FROM sources WHERE id=?")
        .bind(source_id.to_string())
        .fetch_one(pool)
        .await
        .expect("domain ID");
    value.parse().expect("valid domain ID")
}

async fn assert_upload(pool: &SqlitePool, id: UploadId, state: &str, received: i64) {
    let row = sqlx::query("SELECT state,received_bytes FROM uploads WHERE id=?")
        .bind(id.to_string())
        .fetch_one(pool)
        .await
        .expect("upload row");
    assert_eq!(row.try_get::<String, _>("state").expect("state"), state);
    assert_eq!(
        row.try_get::<i64, _>("received_bytes").expect("received"),
        received
    );
}

async fn upload_count(pool: &SqlitePool) -> i64 {
    sqlx::query_scalar("SELECT count(*) FROM uploads")
        .fetch_one(pool)
        .await
        .expect("upload count")
}

fn write_final(fixture: &Fixture, relative: &str, bytes: &[u8]) {
    let path = fixture.artifact_root.join(relative);
    fs::create_dir_all(path.parent().expect("artifact parent")).expect("create artifact parent");
    fs::write(path, bytes).expect("write final artifact");
}

fn note_declaration() -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: "smart_note".into(),
        kind: ArtifactKind::SmartNote,
        path: "note.md".into(),
        required: true,
        when: ArtifactWhen::OnSuccess,
        max_files: None,
        max_bytes: 1024,
    }
}

fn log_declaration() -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: "task_log".into(),
        kind: ArtifactKind::TaskLog,
        path: "task.ndjson".into(),
        required: true,
        when: ArtifactWhen::Always,
        max_files: None,
        max_bytes: 1024,
    }
}

fn bindings_json() -> String {
    serde_json::to_string(&TaskInputBindings::DocumentAcquire {
        source: TaskInputReference::Source,
    })
    .expect("bindings")
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut output, "{byte:02x}").expect("write digest");
    }
    Sha256Digest::parse(output).expect("digest")
}

fn wire_state(state: UploadState) -> &'static str {
    match state {
        UploadState::Receiving => "receiving",
        UploadState::Verified => "verified",
        UploadState::Moved => "moved",
    }
}
