mod assertions;
mod fixture;
mod http;
mod process;

use std::{
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use flori_core::{
    AiTool, CreateJobRequest, CreateUploadSource, CreatedJob, DomainId, JobId, JobInputs,
    PipelineId, RunnerId, SourceKind,
};
use flori_runner::{DaemonConfig, RunnerClient};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{net::TcpListener, task::JoinHandle};

use self::{
    assertions::VerifyContext,
    process::{AiDaemon, DockerRunner},
};

struct Harness {
    root: PathBuf,
    sqlite_path: PathBuf,
    artifact_root: PathBuf,
    pool: SqlitePool,
    address: SocketAddr,
    domain_id: DomainId,
    pipeline_id: PipelineId,
    media_runner_id: RunnerId,
    qoder_runner_id: RunnerId,
    server: JoinHandle<()>,
    preserve: bool,
}

impl Harness {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "flori-pdf-product-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&root).expect("create PDF product root");
        let sqlite_path = root.join("flori.sqlite");
        let artifact_root = root.join("artifacts");
        let store = Arc::new(Store::open(&sqlite_path).await.expect("empty SQLite"));
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&sqlite_path)
                .foreign_keys(true),
        )
        .await
        .expect("inspection pool");
        let artifacts = Arc::new(
            NasArtifactStore::new(&artifact_root, 128 * 1024 * 1024).expect("empty NAS root"),
        );
        let (domain_id, pipeline_id, media_runner_id, qoder_runner_id) =
            fixture::seed(&store, &pool).await;
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind server");
        let address = listener.local_addr().expect("server address");
        let download_base = format!("http://{address}");
        let server_store = Arc::clone(&store);
        let server = tokio::spawn(async move {
            axum::serve(
                listener,
                flori_server::app(server_store, artifacts, download_base, 60_000).expect("app"),
            )
            .await
            .expect("serve");
        });
        Self {
            root,
            sqlite_path,
            artifact_root,
            pool,
            address,
            domain_id,
            pipeline_id,
            media_runner_id,
            qoder_runner_id,
            server,
            preserve: false,
        }
    }

    fn base_url(&self) -> String {
        format!("http://{}", self.address)
    }

    fn preserve(&mut self) {
        self.preserve = true;
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        self.server.abort();
        if !self.preserve {
            let _ = fs::remove_dir_all(&self.root);
        }
    }
}

pub(super) async fn run(image: &str) {
    let mut harness = Harness::new().await;
    let fixture =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/vnext/digital-paper.pdf");
    let pdf = fs::read(&fixture).expect("read canonical digital PDF fixture");
    let created = http::upload_pdf(
        harness.address,
        &CreateUploadSource {
            request_key: "pdf-product-upload".into(),
            kind: SourceKind::PdfUpload,
            title: Some("Flori vNext Golden Paper".into()),
            domain_id: harness.domain_id,
            collection_ids: Vec::new(),
            file_sha256: fixture::digest(&pdf),
        },
        "digital-paper.pdf",
        &pdf,
    )
    .await;
    let job: CreatedJob = http::post_json(
        harness.address,
        &format!("/api/v1/sources/{}/jobs", created.source_id),
        &CreateJobRequest {
            request_key: "pdf-product-job".into(),
            pipeline_id: harness.pipeline_id,
            inputs: JobInputs { translate: false },
        },
    )
    .await;
    let media = RunnerClient::register(
        &harness.base_url(),
        fixture::MEDIA_REGISTRATION,
        &fixture::media_capabilities(),
    )
    .await
    .expect("register media Runner");
    assert_eq!(media.runner_id, harness.media_runner_id);
    let qoder = RunnerClient::register(
        &harness.base_url(),
        fixture::QODER_REGISTRATION,
        &fixture::qoder_capabilities(),
    )
    .await
    .expect("register Qoder Runner");
    assert_eq!(qoder.runner_id, harness.qoder_runner_id);

    let mut media_process =
        DockerRunner::start(&harness.root, image, &harness.base_url(), &media.token);
    wait_for_task(&harness.pool, job.job_id, "extract", &mut media_process).await;
    let document =
        assertions::load_document(&harness.pool, &harness.artifact_root, job.job_id).await;
    let media_log = media_process.log_path.clone();
    drop(media_process);

    let (envelope, expected) = fixture::note(&document);
    let fake = fixture::write_qoder(&harness.root, &envelope);
    let qoder_client =
        RunnerClient::new(&harness.base_url(), qoder.token.clone()).expect("Qoder Runner client");
    let daemon = AiDaemon::start(
        qoder_client,
        DaemonConfig {
            tool: AiTool::QoderCli,
            executable: fake.executable,
            home: fake.home,
            tool_config_home: fake.config,
            work_root: fake.work,
            model: fixture::MODEL.into(),
            effort: fixture::EFFORT.into(),
            renew_interval: Duration::from_millis(100),
            max_output_bytes: 1024 * 1024,
        },
    );
    wait_for_job(&harness.pool, job.job_id, &daemon).await;
    daemon.stop().await;

    let receipt = assertions::verify_and_write_receipt(&VerifyContext {
        pool: &harness.pool,
        sqlite_path: &harness.sqlite_path,
        artifact_root: &harness.artifact_root,
        address: harness.address,
        source_id: created.source_id,
        job_id: job.job_id,
        expected: &expected,
        media_log: &media_log,
        captured_prompt: &fake.captured_prompt,
        secrets: [&media.token, &qoder.token],
    })
    .await;
    println!("FLORI_PDF_PRODUCT_RECEIPT={receipt}");
    harness.preserve();
}

async fn wait_for_task(pool: &SqlitePool, job_id: JobId, task_key: &str, media: &mut DockerRunner) {
    tokio::time::timeout(Duration::from_secs(120), async {
        loop {
            let row: (String, Option<String>) =
                sqlx::query_as("SELECT state,error_code FROM tasks WHERE job_id=? AND task_key=?")
                    .bind(job_id.to_string())
                    .bind(task_key)
                    .fetch_one(pool)
                    .await
                    .expect("Task state");
            match row.0.as_str() {
                "succeeded" => break,
                "failed" | "canceled" => {
                    let tasks: Vec<(String, String, Option<String>)> = sqlx::query_as(
                        "SELECT task_key,state,error_code FROM tasks WHERE job_id=? ORDER BY task_key",
                    )
                    .bind(job_id.to_string())
                    .fetch_all(pool)
                    .await
                    .expect("failed Task summary");
                    let log = fs::read_to_string(&media.log_path).unwrap_or_default();
                    panic!("{task_key} ended as {} with {:?}; tasks={tasks:?}; media={log}", row.0, row.1);
                }
                _ => media.assert_running(),
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("runner-media did not finish the PDF fixture in 120 seconds");
}

async fn wait_for_job(pool: &SqlitePool, job_id: JobId, daemon: &AiDaemon) {
    tokio::time::timeout(Duration::from_secs(30), async {
        loop {
            let state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
                .bind(job_id.to_string())
                .fetch_one(pool)
                .await
                .expect("Job state");
            match state.as_str() {
                "succeeded" => break,
                "failed" | "canceled" => panic!("PDF product Job ended as {state}"),
                _ => daemon.assert_running(),
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    })
    .await
    .expect("AI validate/publish chain did not finish in 30 seconds");
}
