use std::{fmt::Write as _, fs, path::PathBuf};

use flori_core::{
    ArtifactId, AttemptId, DomainId, EvidenceEntry, EvidenceId, EvidenceLocator, EvidenceManifest,
    EvidenceManifestSchema, JobId, PdfRect, PipelineId, PipelineRevisionId, PromptSnapshotId,
    SourceId, TaskId,
};
use flori_store::{
    Store,
    artifact::{NasArtifactStore, retained_artifact_path, task_artifact_path},
};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};

struct Harness {
    root: PathBuf,
    store: Store,
    artifacts: NasArtifactStore,
    pool: SqlitePool,
    source_id: SourceId,
    revision_id: PipelineRevisionId,
}

struct SeededJob {
    job_id: JobId,
    publish_id: TaskId,
    evidence_id: EvidenceId,
    evidence_path: PathBuf,
    note_path: PathBuf,
}

impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-knowledge-{}", JobId::generate()));
        fs::create_dir(&root).expect("test root");
        let database = root.join("flori.sqlite");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let artifacts =
            NasArtifactStore::new(root.join("artifacts"), 1024 * 1024).expect("artifact store");
        let domain_id = DomainId::generate();
        let pipeline_id = PipelineId::generate();
        let revision_id = PipelineRevisionId::generate();
        let source_id = SourceId::generate();
        sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,?,'',0,0)")
            .bind(domain_id.to_string()).bind(format!("domain-{domain_id}")).bind("Research")
            .execute(&pool).await.expect("domain");
        sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,0)")
            .bind(pipeline_id.to_string())
            .bind("pdf")
            .execute(&pool)
            .await
            .expect("pipeline");
        sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'pdf: {}',0)")
            .bind(revision_id.to_string()).bind(pipeline_id.to_string()).bind("0".repeat(64))
            .execute(&pool).await.expect("revision");
        sqlx::query("INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_upload','upload:test','Projection Paper',?,?,?,0,0)")
            .bind(source_id.to_string()).bind(domain_id.to_string()).bind(format!("source-{source_id}"))
            .bind("1".repeat(64)).execute(&pool).await.expect("source");
        Self {
            root,
            store,
            artifacts,
            pool,
            source_id,
            revision_id,
        }
    }

    async fn seed_job(&self, term: &str, malformed_evidence: bool) -> SeededJob {
        let job_id = JobId::generate();
        let extract_id = TaskId::generate();
        let note_id = TaskId::generate();
        let validate_id = TaskId::generate();
        let publish_id = TaskId::generate();
        sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms) VALUES(?,?,?,'initial','running',?,?,'{}','{\"translate\":false}',?,?,1,1)")
            .bind(job_id.to_string()).bind(self.source_id.to_string()).bind(self.revision_id.to_string())
            .bind(PromptSnapshotId::generate().to_string()).bind("2".repeat(64)).bind(format!("job-{job_id}"))
            .bind("3".repeat(64)).execute(&self.pool).await.expect("job");
        for (id, key, executor, state) in [
            (extract_id, "extract", "document.extract", "succeeded"),
            (note_id, "note", "ai.document_note", "succeeded"),
            (validate_id, "validate", "core.validate", "succeeded"),
            (publish_id, "publish", "core.publish", "ready"),
        ] {
            sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms) VALUES(?,?,?,?, '{}','{}',?,1,1000)")
                .bind(id.to_string()).bind(job_id.to_string()).bind(key).bind(executor).bind(state)
                .execute(&self.pool).await.expect("task");
        }

        let source_artifact_id = ArtifactId::generate();
        self.insert_artifact(
            job_id,
            extract_id,
            source_artifact_id,
            "source",
            "source_original",
            "application/pdf",
            "source.pdf",
            b"%PDF-1.7",
            true,
        )
        .await;
        let evidence_id = EvidenceId::generate();
        let evidence = EvidenceManifest {
            schema: EvidenceManifestSchema::V1,
            items: vec![EvidenceEntry {
                evidence_id,
                source_artifact_id,
                locator: EvidenceLocator::Pdf {
                    page: 1,
                    bbox: PdfRect {
                        x1: 1.0,
                        y1: 2.0,
                        x2: 3.0,
                        y2: 4.0,
                    },
                },
                quote: "A verified source quote".into(),
            }],
        };
        let mut evidence_bytes = serde_json::to_vec(&evidence).expect("evidence json");
        if malformed_evidence {
            evidence_bytes = String::from_utf8(evidence_bytes)
                .expect("utf8")
                .replacen("{\"schema\"", "{\"unknown\":true,\"schema\"", 1)
                .into_bytes();
        }
        let marker = format!("[[evidence:{evidence_id}]]");
        let note =
            format!("# {term}\n\n## 来源事实\n\n{term} fact. {marker}\n\n## AI 分析\n\nAnalysis.");
        let summary = format!("{term} summary. {marker}");
        let note_artifact_id = ArtifactId::generate();
        let note_path = self
            .insert_artifact(
                job_id,
                note_id,
                note_artifact_id,
                "smart_note",
                "smart_note",
                "text/markdown",
                "smart-note.md",
                note.as_bytes(),
                false,
            )
            .await;
        self.insert_artifact(
            job_id,
            note_id,
            ArtifactId::generate(),
            "summary",
            "summary",
            "text/markdown",
            "summary.md",
            summary.as_bytes(),
            false,
        )
        .await;
        let evidence_path = self
            .insert_artifact(
                job_id,
                validate_id,
                ArtifactId::generate(),
                "evidence",
                "evidence",
                "application/json",
                "evidence.json",
                &evidence_bytes,
                false,
            )
            .await;
        SeededJob {
            job_id,
            publish_id,
            evidence_id,
            evidence_path,
            note_path,
        }
    }

    #[allow(clippy::too_many_arguments)]
    async fn insert_artifact(
        &self,
        job_id: JobId,
        task_id: TaskId,
        artifact_id: ArtifactId,
        name: &str,
        kind: &str,
        media_type: &str,
        file_name: &str,
        body: &[u8],
        retained: bool,
    ) -> PathBuf {
        let relative = if retained {
            retained_artifact_path(self.source_id, artifact_id, file_name)
        } else {
            task_artifact_path(self.source_id, job_id, task_id, artifact_id, file_name)
        }
        .expect("artifact path");
        let path = self.root.join("artifacts").join(&relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("artifact parent");
        fs::write(&path, body).expect("artifact bytes");
        sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,'materialized',?,?,?,?,?,?,?, ?,0)")
            .bind(artifact_id.to_string()).bind(self.source_id.to_string()).bind(job_id.to_string())
            .bind(task_id.to_string()).bind(name).bind(kind).bind(media_type).bind(file_name)
            .bind(i64::try_from(body.len()).expect("size")).bind(digest(body)).bind(relative)
            .bind(if retained { "source" } else { "published" })
            .execute(&self.pool).await.expect("artifact row");
        path
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

fn digest(body: &[u8]) -> String {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(body) {
        write!(&mut value, "{byte:02x}").expect("digest");
    }
    value
}

async fn publish(
    harness: &Harness,
    job: &SeededJob,
    now_ms: i64,
) -> Result<(), flori_store::StoreError> {
    harness
        .store
        .publish_job_with_projection(
            &harness.artifacts,
            job.job_id,
            job.publish_id,
            AttemptId::generate(),
            now_ms,
        )
        .await
}

#[tokio::test]
async fn valid_publish_builds_current_evidence_and_fts_then_switches_deterministically() {
    let harness = Harness::new().await;
    let first = harness.seed_job("firsttoken", false).await;
    publish(&harness, &first, 10).await.expect("first publish");
    let hit: (String, String) = sqlx::query_as(
        "SELECT sc.job_id,sce.evidence_id FROM search_chunks sc JOIN sources s ON s.current_job_id=sc.job_id JOIN search_chunk_evidence sce ON sce.chunk_id=sc.chunk_id WHERE search_chunks MATCH '\"firsttoken\"' LIMIT 1",
    ).fetch_one(&harness.pool).await.expect("current FTS hit");
    assert_eq!(
        hit,
        (first.job_id.to_string(), first.evidence_id.to_string())
    );

    let second = harness.seed_job("secondtoken", false).await;
    publish(&harness, &second, 20)
        .await
        .expect("second publish");
    let pointers: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("pointers");
    assert_eq!(
        pointers,
        (second.job_id.to_string(), first.job_id.to_string())
    );
    let projected_jobs: Vec<String> =
        sqlx::query_scalar("SELECT DISTINCT job_id FROM search_chunks ORDER BY job_id")
            .fetch_all(&harness.pool)
            .await
            .expect("projected jobs");
    assert_eq!(projected_jobs, [second.job_id.to_string()]);
    let evidence_jobs: Vec<String> = sqlx::query_scalar("SELECT DISTINCT job_id FROM evidence")
        .fetch_all(&harness.pool)
        .await
        .expect("evidence jobs");
    assert_eq!(evidence_jobs, [second.job_id.to_string()]);
}

#[tokio::test]
async fn projection_failure_rolls_back_current_and_all_previous_projection_rows() {
    let harness = Harness::new().await;
    let first = harness.seed_job("stabletoken", false).await;
    publish(&harness, &first, 10).await.expect("first publish");
    let malformed = harness.seed_job("badtok", true).await;
    let error = publish(&harness, &malformed, 20)
        .await
        .expect_err("unknown evidence field");
    assert_eq!(error.code(), flori_core::ErrorCode::EvidenceInvalid);
    let current: String = sqlx::query_scalar("SELECT current_job_id FROM sources WHERE id=?")
        .bind(harness.source_id.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("current");
    assert_eq!(current, first.job_id.to_string());
    let jobs: Vec<String> = sqlx::query_scalar("SELECT DISTINCT job_id FROM search_chunks")
        .fetch_all(&harness.pool)
        .await
        .expect("projection");
    assert_eq!(jobs, [first.job_id.to_string()]);
    let attempt_count: i64 = sqlx::query_scalar("SELECT count(*) FROM attempts WHERE task_id=?")
        .bind(malformed.publish_id.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("attempts");
    assert_eq!(attempt_count, 0);
}

#[tokio::test]
async fn missing_or_mutated_nas_artifacts_fail_before_publish_commit() {
    for remove in [true, false] {
        let harness = Harness::new().await;
        let job = harness.seed_job("drifttoken", false).await;
        if remove {
            fs::remove_file(&job.evidence_path).expect("remove evidence");
        } else {
            fs::write(&job.note_path, b"mutated note bytes").expect("mutate note");
        }
        let error = publish(&harness, &job, 10).await.expect_err("NAS drift");
        assert_eq!(error.code(), flori_core::ErrorCode::DigestMismatch);
        let current: Option<String> =
            sqlx::query_scalar("SELECT current_job_id FROM sources WHERE id=?")
                .bind(harness.source_id.to_string())
                .fetch_one(&harness.pool)
                .await
                .expect("current");
        assert_eq!(current, None);
        let rows: i64 = sqlx::query_scalar("SELECT count(*) FROM search_chunks")
            .fetch_one(&harness.pool)
            .await
            .expect("projection rows");
        assert_eq!(rows, 0);
    }
}
