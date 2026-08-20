use std::{fmt::Write, fs, path::PathBuf};

use flori_core::{
    AttemptId, DocumentStructure, DomainId, Executor, JobId, PipelineId, PipelineRevisionId,
    RunnerId, Sha256Digest, SourceId, TaskId, TermsManifest, UploadId, validate_pdf_evidence,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

mod seed;

const DOCUMENT: &str = include_str!("../../../../tests/fixtures/vnext/expected/document.json");
const NOTE: &str = include_str!("../../../../tests/fixtures/vnext/expected/pdf-smart-note.md");
const SUMMARY: &str = include_str!("../../../../tests/fixtures/vnext/expected/pdf-summary.md");
const TERMS: &str = include_str!("../../../../tests/fixtures/vnext/expected/terms.json");

pub(super) struct Fixture {
    directory: PathBuf,
    pub(super) root: PathBuf,
    pub(super) store: Store,
    pub(super) pool: SqlitePool,
    pub(super) source_id: SourceId,
    pub(super) job_id: JobId,
    pub(super) validate_id: TaskId,
    publish_id: TaskId,
}

pub(super) struct Reserved {
    pub(super) attempt_id: AttemptId,
    pub(super) upload_id: UploadId,
    pub(super) final_path: String,
    pub(super) bytes: Vec<u8>,
}

impl Fixture {
    pub(super) async fn new() -> Self {
        let directory = std::env::temp_dir().join(format!(
            "flori-core-validation-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&directory).expect("test directory");
        let root = directory.join("artifacts");
        let database = directory.join("flori.sqlite");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let domain_id = DomainId::generate();
        let pipeline_id = PipelineId::generate();
        let revision_id = PipelineRevisionId::generate();
        let source_id = SourceId::generate();
        let job_id = JobId::generate();
        let upstream_id = TaskId::generate();
        let upstream_attempt = AttemptId::generate();
        let runner_id = RunnerId::generate();
        let validate_id = TaskId::generate();
        let publish_id = TaskId::generate();
        seed::foundation(
            &pool,
            domain_id,
            pipeline_id,
            revision_id,
            source_id,
            job_id,
        )
        .await;
        sqlx::query(
            "INSERT INTO runners(id,name,state,config_revision,max_concurrency,tags_json,tools_json, \
             ai_models_json,created_at_ms,updated_at_ms) VALUES(?,?,'enabled',1,1,'[]','[]','[]',0,0)",
        )
        .bind(runner_id.to_string())
        .bind(format!("runner-{runner_id}"))
        .execute(&pool)
        .await
        .expect("runner");
        seed::task(
            &pool,
            job_id,
            upstream_id,
            "upstream",
            Executor::DocumentExtract,
            "succeeded",
            Vec::new(),
            Vec::new(),
        )
        .await;
        sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms,finished_at_ms) VALUES(?,?,1,?,'succeeded',0,0,0,0)",
        )
        .bind(upstream_attempt.to_string())
        .bind(upstream_id.to_string())
        .bind(runner_id.to_string())
        .execute(&pool)
        .await
        .expect("upstream attempt");
        sqlx::query("UPDATE tasks SET current_attempt_id=? WHERE id=?")
            .bind(upstream_attempt.to_string())
            .bind(upstream_id.to_string())
            .execute(&pool)
            .await
            .expect("upstream current attempt");
        seed::inputs(
            &pool,
            &root,
            source_id,
            job_id,
            upstream_id,
            upstream_attempt,
        )
        .await;
        seed::task(
            &pool,
            job_id,
            validate_id,
            "validate",
            Executor::CoreValidate,
            "ready",
            vec!["upstream".to_owned()],
            vec![seed::evidence_declaration()],
        )
        .await;
        seed::task(
            &pool,
            job_id,
            publish_id,
            "publish",
            Executor::CorePublish,
            "pending",
            vec!["validate".to_owned()],
            Vec::new(),
        )
        .await;
        Self {
            directory,
            root,
            store,
            pool,
            source_id,
            job_id,
            validate_id,
            publish_id,
        }
    }

    pub(super) fn artifacts(&self) -> NasArtifactStore {
        NasArtifactStore::new(&self.root, 1024 * 1024).expect("artifact store")
    }

    pub(super) async fn assert_completed(&self, reserved: &Reserved) {
        let state: (String, String) = sqlx::query_as(
            "SELECT t.state,a.state FROM tasks t JOIN attempts a ON a.id=t.current_attempt_id WHERE t.id=?",
        )
        .bind(self.validate_id.to_string())
        .fetch_one(&self.pool)
        .await
        .expect("states");
        assert_eq!(state, ("succeeded".to_owned(), "succeeded".to_owned()));
        let upload_count: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads WHERE owner_id=?")
            .bind(reserved.attempt_id.to_string())
            .fetch_one(&self.pool)
            .await
            .expect("upload count");
        assert_eq!(upload_count, 0);
        let artifact: (String, String) =
            sqlx::query_as("SELECT relative_path,sha256 FROM artifacts WHERE attempt_id=?")
                .bind(reserved.attempt_id.to_string())
                .fetch_one(&self.pool)
                .await
                .expect("evidence artifact");
        assert_eq!(artifact.0, reserved.final_path);
        assert_eq!(artifact.1, digest(&reserved.bytes).as_str());
        assert_eq!(
            fs::read(self.root.join(&artifact.0)).expect("evidence file"),
            reserved.bytes
        );
        let publish: String = sqlx::query_scalar("SELECT state FROM tasks WHERE id=?")
            .bind(self.publish_id.to_string())
            .fetch_one(&self.pool)
            .await
            .expect("publish state");
        assert_eq!(publish, "ready");
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove fixture");
    }
}

pub(super) fn evidence_bytes() -> Vec<u8> {
    let document: DocumentStructure = serde_json::from_str(DOCUMENT).expect("document");
    let terms: TermsManifest = serde_json::from_str(TERMS).expect("terms");
    let manifest = validate_pdf_evidence(&document, &terms, NOTE, SUMMARY).expect("evidence");
    serde_json::to_vec(&manifest).expect("evidence bytes")
}

pub(super) fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("format digest");
    }
    Sha256Digest::parse(value).expect("digest")
}
