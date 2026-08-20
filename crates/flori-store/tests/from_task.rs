use std::{collections::BTreeMap, fmt::Write, fs, path::PathBuf};

use flori_core::{
    AiModelCapability, AiRunnerSelection, ArtifactKind, ArtifactWhen, AttemptId, CreateRunnerSlot,
    DomainId, ErrorCode, JobId, JobInputs, JobTrigger, PendingMaterializeCommit, PipelineId,
    PipelineRevisionId, PromptSnapshot, PromptSnapshotId, PromptSnapshotProfile,
    PromptSnapshotPrompt, RegisterRunnerRequest, RerunJobRequest, RerunMode, RunnerId, RunnerTool,
    RunnerToolCapability, Sha256Digest, SourceId, SourceKind, TaskId, UploadState,
};
use flori_pipeline::{Compilation, compile};
use flori_store::{
    CreateJob, CreateSource, Store,
    artifact::{NasArtifactStore, UploadRecord, task_artifact_path},
};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};

const PIPELINE: &str = r#"
acquire:
  executor: document.acquire
  with: { source: $source }
  tags: [media]
  timeout: 1m
  artifacts:
    - { name: original, kind: source_original, path: output/source.pdf, required: true, when: on_success, max_bytes: 1024 }
    - { name: log, kind: task_log, path: logs/task.ndjson, required: true, when: always, max_bytes: 1024 }
left:
  executor: document.extract
  with: { pdf: $needs.acquire.original }
  needs: [acquire]
  tags: [media]
  timeout: 1m
  artifacts:
    - { name: structure, kind: document_structure, path: output/left.json, required: true, when: on_success, max_bytes: 1024 }
right:
  executor: document.extract
  with: { pdf: $needs.acquire.original }
  needs: [acquire]
  tags: [media]
  timeout: 1m
  artifacts:
    - { name: structure, kind: document_structure, path: output/right.json, required: true, when: on_success, max_bytes: 1024 }
translate:
  executor: ai.document_translate
  with: { document: $needs.right.structure, prompt: $prompts.document_translate }
  needs: [right]
  rules:
    - if: $job.translate == true
  tags: [ai]
  timeout: 1m
  artifacts:
    - { name: translation, kind: translation, path: output/translation.md, required: true, when: on_success, max_bytes: 1024 }
    - { name: audit, kind: ai_audit, path: logs/ai-audit.json, required: true, when: always, max_bytes: 1024 }
join:
  executor: core.validate
  with: { source: $needs.left.structure, notes: $needs.right }
  needs: [left, right, translate]
  timeout: 1m
  artifacts:
    - { name: evidence, kind: evidence, path: output/evidence.json, required: true, when: on_success, max_bytes: 1024 }
publish:
  executor: core.publish
  with: { validated: $needs.join.evidence }
  needs: [join]
  timeout: 1m
  artifacts: []
"#;

struct TestDatabase {
    directory: PathBuf,
    path: PathBuf,
}

impl TestDatabase {
    fn new() -> Self {
        let directory = std::env::temp_dir().join(format!("flori-from-task-{}", JobId::generate()));
        fs::create_dir(&directory).expect("test directory");
        Self {
            path: directory.join("flori.db"),
            directory,
        }
    }

    async fn pool(&self) -> SqlitePool {
        SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&self.path)
                .foreign_keys(true),
        )
        .await
        .expect("test pool")
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
    artifacts: NasArtifactStore,
    artifact_root: PathBuf,
    compilation: Compilation,
    source_id: SourceId,
    job_id: JobId,
    revision_id: PipelineRevisionId,
    task_ids: BTreeMap<String, TaskId>,
    source_files: BTreeMap<String, (PathBuf, Vec<u8>)>,
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut output, "{byte:02x}").expect("write digest");
    }
    Sha256Digest::parse(output).expect("digest")
}

fn error<T>(result: Result<T, flori_store::StoreError>) -> ErrorCode {
    match result {
        Ok(_) => panic!("expected Store error"),
        Err(error) => error.code(),
    }
}

async fn foundation(database: &TestDatabase) -> Foundation {
    let store = Store::open(&database.path).await.expect("store");
    let pool = database.pool().await;
    let artifact_root = database.directory.join("artifacts");
    let artifacts = NasArtifactStore::new(&artifact_root, 1024 * 1024).expect("NAS store");
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
    let compilation = compile("branch", PIPELINE.as_bytes()).expect("pipeline");
    let pipeline_id = PipelineId::generate();
    let revision_id = PipelineRevisionId::generate();
    store
        .register_pipeline_revision(pipeline_id, revision_id, &compilation, "test", PIPELINE, 1)
        .await
        .expect("revision");
    let source_id = store
        .create_source(CreateSource {
            kind: SourceKind::PdfUrl,
            canonical_ref: "https://example.test/source.pdf",
            title: None,
            domain_id,
            request_key: "source",
            request_sha256: &"a".repeat(64),
            created_at_ms: 2,
        })
        .await
        .expect("source");
    let profile = "profile";
    let prompt = "translate";
    let snapshot = PromptSnapshot {
        profile: PromptSnapshotProfile {
            domain_id,
            profile_text: profile.into(),
            sha256: digest(profile.as_bytes()),
        },
        prompts: vec![PromptSnapshotPrompt {
            key: "document_translate".into(),
            content: prompt.into(),
            sha256: digest(prompt.as_bytes()),
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
                request_key: "base-job",
                request_sha256: &"b".repeat(64),
                inputs: JobInputs { translate: true },
                created_at_ms: 3,
            },
            &compilation,
        )
        .await
        .expect("base job");
    let registration = digest(b"base-runner-registration");
    let base_runner_id = store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: format!("base-runner-{job_id}"),
                tags: vec!["ai".into(), "media".into()],
                max_concurrency: 1,
                default_model: Some("model-a".into()),
                default_effort: Some("high".into()),
            },
            &registration,
            100,
            3,
        )
        .await
        .expect("base runner slot");
    store
        .register_runner(
            &registration,
            &digest(b"base-runner-token"),
            &RegisterRunnerRequest {
                tools: vec![
                    RunnerToolCapability {
                        tool: RunnerTool::PdfExtractor,
                        version: "1".into(),
                    },
                    RunnerToolCapability {
                        tool: RunnerTool::QoderCli,
                        version: "1".into(),
                    },
                ],
                ai_models: vec![AiModelCapability {
                    model: "model-a".into(),
                    efforts: vec!["high".into()],
                }],
            },
            4,
        )
        .await
        .expect("base runner registration");
    let mut task_ids = BTreeMap::new();
    let mut source_files = BTreeMap::new();
    let rows = sqlx::query("SELECT id,task_key,spec_json FROM tasks WHERE job_id=?")
        .bind(job_id.to_string())
        .fetch_all(&pool)
        .await
        .expect("tasks");
    for row in rows {
        let task_id: TaskId = row
            .try_get::<String, _>("id")
            .expect("task ID")
            .parse()
            .expect("typed task ID");
        let task_key: String = row.try_get("task_key").expect("task key");
        let spec: flori_core::CompiledTaskSpec =
            serde_json::from_str(row.try_get("spec_json").expect("spec JSON")).expect("spec");
        let attempt_id = AttemptId::generate();
        sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms,finished_at_ms) \
             VALUES(?,?,1,?,'succeeded',10,0,4,5)",
        )
        .bind(attempt_id.to_string())
        .bind(task_id.to_string())
        .bind(base_runner_id.to_string())
        .execute(&pool)
        .await
        .expect("attempt");
        sqlx::query(
            "UPDATE tasks SET state='succeeded',current_attempt_id=?,started_at_ms=4, \
             finished_at_ms=5 WHERE id=?",
        )
        .bind(attempt_id.to_string())
        .bind(task_id.to_string())
        .execute(&pool)
        .await
        .expect("succeed task");
        for declaration in spec
            .artifacts
            .iter()
            .filter(|item| item.when == ArtifactWhen::OnSuccess && item.required)
        {
            let artifact_id = flori_core::ArtifactId::generate();
            let declared_path = PathBuf::from(&declaration.path);
            let file_name = declared_path
                .file_name()
                .and_then(|value| value.to_str())
                .expect("artifact basename")
                .to_owned();
            let relative = task_artifact_path(source_id, job_id, task_id, artifact_id, &file_name)
                .expect("artifact path");
            let bytes = format!("{task_key}:{}", declaration.name).into_bytes();
            let path = artifact_root.join(&relative);
            fs::create_dir_all(path.parent().expect("artifact parent")).expect("artifact parent");
            fs::write(&path, &bytes).expect("artifact bytes");
            let media_type = media_type(declaration.kind);
            sqlx::query(
                "INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind, \
                 media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) \
                 VALUES(?,?,?,?,?,'produced',?,?,?,?,?,?,?,?,5)",
            )
            .bind(artifact_id.to_string())
            .bind(source_id.to_string())
            .bind(job_id.to_string())
            .bind(task_id.to_string())
            .bind(attempt_id.to_string())
            .bind(&declaration.name)
            .bind(wire(&declaration.kind))
            .bind(media_type)
            .bind(&file_name)
            .bind(bytes.len() as i64)
            .bind(digest(&bytes).as_str())
            .bind(&relative)
            .bind(if declaration.kind == ArtifactKind::SourceOriginal {
                "source"
            } else {
                "published"
            })
            .execute(&pool)
            .await
            .expect("artifact row");
            source_files.insert(format!("{task_key}/{}", declaration.name), (path, bytes));
        }
        task_ids.insert(task_key, task_id);
    }
    sqlx::query("UPDATE jobs SET state='succeeded',started_at_ms=4,finished_at_ms=6 WHERE id=?")
        .bind(job_id.to_string())
        .execute(&pool)
        .await
        .expect("succeed job");
    sqlx::query("UPDATE sources SET current_job_id=?,updated_at_ms=6 WHERE id=?")
        .bind(job_id.to_string())
        .bind(source_id.to_string())
        .execute(&pool)
        .await
        .expect("publish base");
    Foundation {
        store,
        pool,
        artifacts,
        artifact_root,
        compilation,
        source_id,
        job_id,
        revision_id,
        task_ids,
        source_files,
    }
}

fn media_type(kind: ArtifactKind) -> &'static str {
    match kind {
        ArtifactKind::SourceOriginal => "application/pdf",
        ArtifactKind::DocumentStructure | ArtifactKind::Evidence => "application/json",
        ArtifactKind::Translation => "text/markdown",
        _ => panic!("unexpected test ArtifactKind"),
    }
}

fn wire(value: &ArtifactKind) -> String {
    serde_json::to_string(value)
        .expect("wire enum")
        .trim_matches('"')
        .to_owned()
}

fn request(key: &str, from: &str, ai_selection: Option<AiRunnerSelection>) -> RerunJobRequest {
    RerunJobRequest {
        request_key: key.into(),
        mode: RerunMode::FromTask,
        from_task_key: Some(from.into()),
        ai_selection,
    }
}

async fn register_pdf_runner(foundation: &Foundation, name: &str) -> RunnerId {
    let registration = digest(format!("{name}-registration").as_bytes());
    let runner_id = foundation
        .store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: name.into(),
                tags: vec!["media".into()],
                max_concurrency: 1,
                default_model: None,
                default_effort: None,
            },
            &registration,
            100,
            7,
        )
        .await
        .expect("runner slot");
    foundation
        .store
        .register_runner(
            &registration,
            &digest(format!("{name}-token").as_bytes()),
            &RegisterRunnerRequest {
                tools: vec![RunnerToolCapability {
                    tool: RunnerTool::PdfExtractor,
                    version: "1".into(),
                }],
                ai_models: Vec::new(),
            },
            8,
        )
        .await
        .expect("register runner");
    runner_id
}

async fn register_ai_runner(foundation: &Foundation, name: &str) -> RunnerId {
    let registration = digest(format!("{name}-registration").as_bytes());
    let runner_id = foundation
        .store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: name.into(),
                tags: vec!["ai".into()],
                max_concurrency: 1,
                default_model: Some("model-a".into()),
                default_effort: Some("high".into()),
            },
            &registration,
            100,
            7,
        )
        .await
        .expect("AI runner slot");
    foundation
        .store
        .register_runner(
            &registration,
            &digest(format!("{name}-token").as_bytes()),
            &RegisterRunnerRequest {
                tools: vec![RunnerToolCapability {
                    tool: RunnerTool::QoderCli,
                    version: "1".into(),
                }],
                ai_models: vec![AiModelCapability {
                    model: "model-a".into(),
                    efforts: vec!["high".into()],
                }],
            },
            8,
        )
        .await
        .expect("register AI runner");
    runner_id
}

async fn pending_commit(foundation: &Foundation, request_key: &str) -> PendingMaterializeCommit {
    let json: String = sqlx::query_scalar(
        "SELECT commit_json FROM uploads WHERE owner_kind='materialize' AND request_key=?",
    )
    .bind(request_key)
    .fetch_one(&foundation.pool)
    .await
    .expect("pending materialize commit");
    serde_json::from_str(&json).expect("strict pending materialize commit")
}

fn pending_upload(pending: &PendingMaterializeCommit, index: usize) -> UploadRecord {
    let artifact = &pending.artifacts[index];
    let task = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .expect("pending artifact task");
    let declaration = task
        .spec
        .artifacts
        .iter()
        .find(|declaration| declaration.name == artifact.name)
        .expect("pending artifact declaration");
    UploadRecord::new(
        artifact.upload_id,
        &artifact.name,
        &artifact.final_relative_path,
        artifact.size_bytes,
        artifact.sha256.clone(),
        &declaration.name,
        declaration.max_bytes,
    )
    .expect("pending upload record")
}

fn source_bytes<'a>(
    foundation: &'a Foundation,
    pending: &PendingMaterializeCommit,
    index: usize,
) -> &'a [u8] {
    let artifact = &pending.artifacts[index];
    let task = pending
        .tasks
        .iter()
        .find(|task| task.task_id == artifact.task_id)
        .expect("pending artifact task");
    &foundation.source_files[&format!("{}/{}", task.task_key, artifact.name)].1
}

fn restore_source_files(foundation: &Foundation) {
    for (path, bytes) in foundation.source_files.values() {
        fs::write(path, bytes).expect("restore source artifact");
    }
}

#[tokio::test]
async fn from_task_materializes_parallel_branches_and_resolver_enforces_origin_state() {
    let database = TestDatabase::new();
    let foundation = foundation(&database).await;
    let command = request("rerun-left", "left", None);
    let job_id = foundation
        .store
        .rerun_from_task(
            &foundation.artifacts,
            foundation.job_id,
            &command,
            &foundation.compilation,
            20,
        )
        .await
        .expect("rerun from left");
    assert_eq!(
        foundation
            .store
            .rerun_from_task(
                &foundation.artifacts,
                foundation.job_id,
                &command,
                &foundation.compilation,
                21,
            )
            .await
            .expect("idempotent response"),
        job_id
    );
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &request("rerun-left", "right", None),
                    &foundation.compilation,
                    22,
                )
                .await,
        ),
        ErrorCode::IdempotencyConflict
    );
    let states: BTreeMap<String, String> =
        sqlx::query("SELECT task_key,state FROM tasks WHERE job_id=?")
            .bind(job_id.to_string())
            .fetch_all(&foundation.pool)
            .await
            .expect("rerun tasks")
            .into_iter()
            .map(|row| {
                (
                    row.try_get("task_key").expect("key"),
                    row.try_get("state").expect("state"),
                )
            })
            .collect();
    assert_eq!(states["acquire"], "skipped");
    assert_eq!(states["left"], "ready");
    assert_eq!(states["right"], "skipped");
    assert_eq!(states["translate"], "skipped");
    assert_eq!(states["join"], "pending");
    assert_eq!(states["publish"], "pending");
    let inputs_json: String = sqlx::query_scalar("SELECT inputs_json FROM jobs WHERE id=?")
        .bind(job_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("inputs");
    assert_eq!(inputs_json, r#"{"translate":true}"#);
    let materialized: Vec<(String, String, String)> = sqlx::query_as(
        "SELECT t.task_key,a.name,a.relative_path FROM artifacts a JOIN tasks t ON t.id=a.task_id \
         WHERE a.job_id=? ORDER BY t.task_key,a.name",
    )
    .bind(job_id.to_string())
    .fetch_all(&foundation.pool)
    .await
    .expect("materialized artifacts");
    assert_eq!(
        materialized
            .iter()
            .map(|(task, name, _)| (task.as_str(), name.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("acquire", "original"),
            ("right", "structure"),
            ("translate", "translation"),
        ]
    );
    for (task, name, relative_path) in &materialized {
        let (_, expected) = &foundation.source_files[&format!("{task}/{name}")];
        assert_eq!(
            fs::read(foundation.artifact_root.join(relative_path)).expect("materialized bytes"),
            *expected
        );
    }
    let runner_id = register_pdf_runner(&foundation, "resolver-runner").await;
    let acquire_task: String =
        sqlx::query_scalar("SELECT id FROM tasks WHERE job_id=? AND task_key='acquire'")
            .bind(job_id.to_string())
            .fetch_one(&foundation.pool)
            .await
            .expect("acquire task");
    let acquire_artifact: String = sqlx::query_scalar(
        "SELECT id FROM artifacts WHERE job_id=? AND task_id=? AND name='original'",
    )
    .bind(job_id.to_string())
    .bind(&acquire_task)
    .fetch_one(&foundation.pool)
    .await
    .expect("acquire artifact");
    let malicious_attempt = AttemptId::generate();
    sqlx::query(
        "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
         last_log_sequence,started_at_ms,finished_at_ms) VALUES(?,?,1,?,'succeeded',30,0,23,23)",
    )
    .bind(malicious_attempt.to_string())
    .bind(&acquire_task)
    .bind(runner_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("malicious attempt");
    sqlx::query("UPDATE artifacts SET origin='produced',attempt_id=? WHERE id=?")
        .bind(malicious_attempt.to_string())
        .bind(&acquire_artifact)
        .execute(&foundation.pool)
        .await
        .expect("produced on skipped");
    assert_eq!(
        error(
            foundation
                .store
                .poll_and_claim(runner_id, 24, 50, "https://flori.example")
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query("UPDATE artifacts SET origin='materialized',attempt_id=NULL WHERE id=?")
        .bind(&acquire_artifact)
        .execute(&foundation.pool)
        .await
        .expect("restore materialized");
    sqlx::query("UPDATE tasks SET state='succeeded',current_attempt_id=? WHERE id=?")
        .bind(malicious_attempt.to_string())
        .bind(&acquire_task)
        .execute(&foundation.pool)
        .await
        .expect("materialized on succeeded");
    assert_eq!(
        error(
            foundation
                .store
                .poll_and_claim(runner_id, 25, 50, "https://flori.example")
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query("UPDATE tasks SET state='skipped',current_attempt_id=NULL WHERE id=?")
        .bind(&acquire_task)
        .execute(&foundation.pool)
        .await
        .expect("restore skipped");
    let claim = foundation
        .store
        .poll_and_claim(runner_id, 26, 50, "https://flori.example")
        .await
        .expect("valid materialized input")
        .expect("left claim");
    assert_eq!(claim.task_key, "left");
    assert!(matches!(
        claim.resolved_inputs,
        flori_core::ResolvedTaskInputs::DocumentExtract { .. }
    ));
}

#[tokio::test]
async fn from_task_recovers_copy_windows_and_rejects_drift_or_a_second_active_plan() {
    let database = TestDatabase::new();
    let foundation = foundation(&database).await;
    for (path, _) in foundation.source_files.values() {
        fs::remove_file(path).expect("hide source artifact");
    }
    let command = request("crash-rerun", "left", None);
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    30,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    let pending = pending_commit(&foundation, "crash-rerun").await;
    assert_eq!(pending.artifacts.len(), 3);
    let first = pending_upload(&pending, 0);
    assert!(
        !foundation
            .artifact_root
            .join(first.staging_relative_path())
            .exists()
    );
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &request("other-rerun", "right", None),
                    &foundation.compilation,
                    31,
                )
                .await,
        ),
        ErrorCode::SourceBusy
    );

    let original_json = serde_json::to_string(&pending).expect("pending JSON");
    let mut tampered = pending.clone();
    tampered.from_task_key = "right".into();
    sqlx::query("UPDATE uploads SET commit_json=? WHERE request_key='crash-rerun'")
        .bind(serde_json::to_string(&tampered).expect("tampered pending JSON"))
        .execute(&foundation.pool)
        .await
        .expect("tamper commit");
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    32,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query("UPDATE uploads SET commit_json=? WHERE request_key='crash-rerun'")
        .bind(&original_json)
        .execute(&foundation.pool)
        .await
        .expect("restore commit");

    sqlx::query("UPDATE pipelines SET current_revision_id=NULL")
        .execute(&foundation.pool)
        .await
        .expect("drift revision");
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    33,
                )
                .await,
        ),
        ErrorCode::PipelineInvalid
    );
    sqlx::query("UPDATE pipelines SET current_revision_id=?")
        .bind(foundation.revision_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("restore revision");
    sqlx::query("UPDATE sources SET current_job_id=NULL WHERE id=?")
        .bind(foundation.source_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("drift source current");
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    34,
                )
                .await,
        ),
        ErrorCode::RerunBoundaryInvalid
    );
    sqlx::query("UPDATE sources SET current_job_id=? WHERE id=?")
        .bind(foundation.job_id.to_string())
        .bind(foundation.source_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("restore source current");
    restore_source_files(&foundation);
    for (path, bytes) in foundation.source_files.values() {
        fs::write(path, vec![b'x'; bytes.len()]).expect("tamper source bytes");
    }
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    35,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    restore_source_files(&foundation);

    let second = pending_upload(&pending, 1);
    let second_bytes = source_bytes(&foundation, &pending, 1);
    foundation
        .artifacts
        .append_chunk(&second, 0, &digest(second_bytes), second_bytes)
        .expect("file ahead of cursor");
    let mut third = pending_upload(&pending, 2);
    let third_bytes = source_bytes(&foundation, &pending, 2);
    foundation
        .artifacts
        .append(&mut third, 0, third_bytes)
        .expect("third staging");
    foundation
        .artifacts
        .verify_staging(&third)
        .expect("verify third staging");
    sqlx::query("UPDATE uploads SET received_bytes=?,state='verified' WHERE id=?")
        .bind(i64::try_from(third.expected_size_bytes()).expect("third size"))
        .bind(pending.artifacts[2].upload_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("persist verified before rename");
    third
        .restore_progress(third.expected_size_bytes(), UploadState::Verified)
        .expect("restore verified record");
    foundation
        .artifacts
        .move_verified(&third)
        .expect("rename before moved state");

    let job_id = foundation
        .store
        .rerun_from_task(
            &foundation.artifacts,
            foundation.job_id,
            &command,
            &foundation.compilation,
            36,
        )
        .await
        .expect("recover every copy window");
    assert_eq!(job_id, pending.job_id);
    let remaining: i64 =
        sqlx::query_scalar("SELECT count(*) FROM uploads WHERE owner_kind='materialize'")
            .fetch_one(&foundation.pool)
            .await
            .expect("remaining uploads");
    assert_eq!(remaining, 0);
    let copied: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE job_id=?")
        .bind(job_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("copied artifacts");
    assert_eq!(copied, 3);
}

#[tokio::test]
async fn from_task_freezes_only_a_rerun_ai_task_and_rejects_runner_config_drift() {
    let database = TestDatabase::new();
    let foundation = foundation(&database).await;
    let runner_id = register_ai_runner(&foundation, "selected-ai-runner").await;
    let selection = AiRunnerSelection {
        task_key: "translate".into(),
        runner_id,
        model: "model-a".into(),
        effort: "high".into(),
        runner_config_revision: 1,
    };
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &request("selection-outside-rerun", "left", Some(selection.clone())),
                    &foundation.compilation,
                    40,
                )
                .await,
        ),
        ErrorCode::RerunBoundaryInvalid
    );
    for (path, _) in foundation.source_files.values() {
        fs::remove_file(path).expect("hide source artifact");
    }
    let command = request("selected-translate", "translate", Some(selection.clone()));
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    41,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    let pending = pending_commit(&foundation, "selected-translate").await;
    sqlx::query("UPDATE runners SET config_revision=2 WHERE id=?")
        .bind(runner_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("drift runner configuration");
    assert_eq!(
        error(
            foundation
                .store
                .rerun_from_task(
                    &foundation.artifacts,
                    foundation.job_id,
                    &command,
                    &foundation.compilation,
                    42,
                )
                .await,
        ),
        ErrorCode::RunnerUnavailable
    );
    sqlx::query("UPDATE runners SET config_revision=1 WHERE id=?")
        .bind(runner_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("restore runner configuration");
    restore_source_files(&foundation);
    let job_id = foundation
        .store
        .rerun_from_task(
            &foundation.artifacts,
            foundation.job_id,
            &command,
            &foundation.compilation,
            43,
        )
        .await
        .expect("finish selected AI rerun");
    assert_eq!(job_id, pending.job_id);
    let frozen: (String, String, String, i64) = sqlx::query_as(
        "SELECT pinned_runner_id,selected_model,selected_effort,runner_config_revision \
         FROM tasks WHERE job_id=? AND task_key='translate'",
    )
    .bind(job_id.to_string())
    .fetch_one(&foundation.pool)
    .await
    .expect("frozen AI selection");
    assert_eq!(
        frozen,
        (runner_id.to_string(), "model-a".into(), "high".into(), 1)
    );
}

#[tokio::test]
async fn from_task_with_no_materialized_files_commits_without_a_fake_upload() {
    let database = TestDatabase::new();
    let foundation = foundation(&database).await;
    let command = request("rerun-everything", "acquire", None);
    let job_id = foundation
        .store
        .rerun_from_task(
            &foundation.artifacts,
            foundation.job_id,
            &command,
            &foundation.compilation,
            50,
        )
        .await
        .expect("rerun every task");
    assert_eq!(
        foundation
            .store
            .rerun_from_task(
                &foundation.artifacts,
                foundation.job_id,
                &command,
                &foundation.compilation,
                51,
            )
            .await
            .expect("idempotent zero-copy rerun"),
        job_id
    );
    let uploads: i64 =
        sqlx::query_scalar("SELECT count(*) FROM uploads WHERE owner_kind='materialize'")
            .fetch_one(&foundation.pool)
            .await
            .expect("materialize uploads");
    let artifacts: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE job_id=?")
        .bind(job_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("materialized artifacts");
    assert_eq!((uploads, artifacts), (0, 0));
    let states: BTreeMap<String, String> =
        sqlx::query("SELECT task_key,state FROM tasks WHERE job_id=?")
            .bind(job_id.to_string())
            .fetch_all(&foundation.pool)
            .await
            .expect("zero-copy tasks")
            .into_iter()
            .map(|row| {
                (
                    row.try_get("task_key").expect("task key"),
                    row.try_get("state").expect("task state"),
                )
            })
            .collect();
    assert_eq!(states["acquire"], "ready");
    assert!(
        states
            .iter()
            .filter(|(key, _)| key.as_str() != "acquire")
            .all(|(_, state)| state == "pending")
    );
}

#[tokio::test]
async fn from_task_rejects_missing_required_or_wrong_media_artifacts() {
    let missing_database = TestDatabase::new();
    let missing = foundation(&missing_database).await;
    sqlx::query("DELETE FROM artifacts WHERE task_id=? AND name='structure'")
        .bind(missing.task_ids["right"].to_string())
        .execute(&missing.pool)
        .await
        .expect("remove required artifact");
    assert_eq!(
        error(
            missing
                .store
                .rerun_from_task(
                    &missing.artifacts,
                    missing.job_id,
                    &request("missing-required", "left", None),
                    &missing.compilation,
                    60,
                )
                .await,
        ),
        ErrorCode::RerunBoundaryInvalid
    );

    let media_database = TestDatabase::new();
    let media = foundation(&media_database).await;
    sqlx::query("UPDATE artifacts SET media_type='text/html' WHERE task_id=? AND name='original'")
        .bind(media.task_ids["acquire"].to_string())
        .execute(&media.pool)
        .await
        .expect("drift media type");
    assert_eq!(
        error(
            media
                .store
                .rerun_from_task(
                    &media.artifacts,
                    media.job_id,
                    &request("wrong-media", "left", None),
                    &media.compilation,
                    61,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
}
