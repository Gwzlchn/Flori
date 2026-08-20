mod assertions;
mod fixture;
mod http;
mod process;

use std::{
    fs,
    net::SocketAddr,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use flori_core::{AiTool, DomainId, JobId, PipelineId, RunnerId, SourceKind};
use flori_runner::{DaemonConfig, ProxyUrl, RunnerClient};
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

pub(super) struct RealConfig {
    pub(super) image: String,
    pub(super) root: PathBuf,
    pub(super) pdf: PathBuf,
    pub(super) executable: PathBuf,
    pub(super) config_home: PathBuf,
    pub(super) model: String,
    pub(super) effort: String,
    pub(super) proxy_url: ProxyUrl,
}

impl Harness {
    async fn new(root: PathBuf, preserve: bool, prompt: &str, model: &str, effort: &str) -> Self {
        fs::create_dir(&root).expect("create PDF product root");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
            .expect("protect PDF product root");
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
            fixture::seed(&store, &pool, prompt, model, effort).await;
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
            preserve,
        }
    }

    fn base_url(&self) -> String {
        format!("http://{}", self.address)
    }

    fn preserve(&mut self) {
        self.preserve = true;
    }
}

struct PrivateQoderRuntime(PathBuf);

impl PrivateQoderRuntime {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "flori-real-qoder-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&root).expect("create private Qoder runtime");
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700))
            .expect("protect private Qoder runtime");
        Self(root)
    }

    fn home(&self) -> PathBuf {
        self.0.join("unused-home")
    }

    fn work(&self) -> PathBuf {
        self.0.join("work")
    }
}

impl Drop for PrivateQoderRuntime {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
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

async fn start_media(harness: &Harness, image: &str) -> (DockerRunner, String) {
    let media = RunnerClient::register(
        &harness.base_url(),
        fixture::MEDIA_REGISTRATION,
        &fixture::media_capabilities(),
    )
    .await
    .expect("register media Runner");
    assert_eq!(media.runner_id, harness.media_runner_id);
    (
        DockerRunner::start(&harness.root, image, &harness.base_url(), &media.token),
        media.token,
    )
}

pub(super) async fn run_scanned(image: &str) {
    let root = std::env::temp_dir().join(format!(
        "flori-pdf-scan-{}",
        flori_core::RequestId::generate()
    ));
    let harness = Harness::new(
        root,
        false,
        fixture::FAKE_PROMPT,
        fixture::MODEL,
        fixture::EFFORT,
    )
    .await;
    let pdf =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/vnext/scanned-paper.pdf");
    let (_, job_id) = harness.upload_job(&pdf, "pdf-scanned").await;
    let (mut media, _) = start_media(&harness, image).await;
    wait_for_failed_job(&harness.pool, job_id, &mut media).await;

    let job: (String, Option<String>) =
        sqlx::query_as("SELECT state,error_code FROM jobs WHERE id=?")
            .bind(job_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("failed scanned PDF Job");
    assert_eq!(
        job,
        ("failed".into(), Some("unsupported_scanned_pdf".into()))
    );
    let tasks: Vec<(String, String, Option<String>, i64)> = sqlx::query_as(
        "SELECT t.task_key,t.state,t.error_code,count(a.id) FROM tasks t LEFT JOIN attempts a ON a.task_id=t.id WHERE t.job_id=? GROUP BY t.id ORDER BY t.task_key",
    )
    .bind(job_id.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("scanned PDF Tasks");
    assert_eq!(
        tasks,
        vec![
            (
                "acquire".into(),
                "failed".into(),
                Some("unsupported_scanned_pdf".into()),
                1
            ),
            (
                "extract".into(),
                "canceled".into(),
                Some("task_canceled".into()),
                0
            ),
            (
                "note".into(),
                "canceled".into(),
                Some("task_canceled".into()),
                0
            ),
            (
                "publish".into(),
                "canceled".into(),
                Some("task_canceled".into()),
                0
            ),
            ("translate".into(), "skipped".into(), None, 0),
            (
                "validate".into(),
                "canceled".into(),
                Some("task_canceled".into()),
                0
            ),
        ]
    );
    let ai_usage: i64 = sqlx::query_scalar("SELECT count(*) FROM ai_usage WHERE job_id=?")
        .bind(job_id.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("AI usage count");
    assert_eq!(ai_usage, 0, "scan rejection must happen before AI");
    let downstream_artifacts: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? AND t.task_key<>'acquire'",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("downstream Artifact count");
    assert_eq!(downstream_artifacts, 0);
    let artifact_kinds: Vec<String> =
        sqlx::query_scalar("SELECT kind FROM artifacts WHERE job_id=? ORDER BY kind")
            .bind(job_id.to_string())
            .fetch_all(&harness.pool)
            .await
            .expect("scan rejection Artifacts");
    assert_eq!(artifact_kinds, vec!["task_log"]);
    let uploads: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads")
        .fetch_one(&harness.pool)
        .await
        .expect("upload ledger count");
    assert_eq!(uploads, 0);
}

pub(super) async fn run_input_matrix(image: &str) {
    let root = std::env::temp_dir().join(format!(
        "flori-pdf-inputs-{}",
        flori_core::RequestId::generate()
    ));
    let harness = Harness::new(
        root,
        false,
        fixture::FAKE_PROMPT,
        fixture::MODEL,
        fixture::EFFORT,
    )
    .await;
    let pdf =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/vnext/digital-paper.pdf");
    let (_, upload) = harness.upload_job(&pdf, "pdf-input-upload").await;
    let (_, direct) = harness
        .remote_job(
            SourceKind::PdfUrl,
            "https://arxiv.org/pdf/1706.03762",
            "pdf-input-url",
        )
        .await;
    let (_, arxiv) = harness
        .remote_job(
            SourceKind::Arxiv,
            "https://arxiv.org/abs/1706.03762",
            "pdf-input-arxiv",
        )
        .await;
    let jobs = [upload, direct, arxiv];
    let (mut media, _) = start_media(&harness, image).await;
    for job_id in jobs {
        wait_for_task(&harness.pool, job_id, "acquire", &mut media).await;
    }

    let rows: Vec<(String, String, String, String, i64)> = sqlx::query_as(
        "SELECT s.kind,j.pipeline_revision_id,r.pipeline_id,t.executor,count(a.id) FROM jobs j JOIN sources s ON s.id=j.source_id JOIN pipeline_revisions r ON r.id=j.pipeline_revision_id JOIN tasks t ON t.job_id=j.id AND t.task_key='acquire' LEFT JOIN artifacts a ON a.task_id=t.id AND a.kind='source_original' WHERE j.id IN (?,?,?) GROUP BY j.id ORDER BY s.kind",
    )
    .bind(upload.to_string())
    .bind(direct.to_string())
    .bind(arxiv.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("PDF input acquire contracts");
    assert_eq!(rows.len(), 3);
    assert_eq!(
        rows.iter().map(|row| row.0.as_str()).collect::<Vec<_>>(),
        ["arxiv", "pdf_upload", "pdf_url"]
    );
    let revision = &rows[0].1;
    for (_, revision_id, pipeline_id, executor, original_count) in &rows {
        assert_eq!(revision_id, revision);
        assert_eq!(pipeline_id, &harness.pipeline_id.to_string());
        assert_eq!(executor, "document.acquire");
        assert_eq!(*original_count, 1);
    }
}

pub(super) async fn run(image: &str) {
    let root = std::env::temp_dir().join(format!(
        "flori-pdf-product-{}",
        flori_core::RequestId::generate()
    ));
    let mut harness = Harness::new(
        root,
        false,
        fixture::FAKE_PROMPT,
        fixture::MODEL,
        fixture::EFFORT,
    )
    .await;
    let fixture =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/vnext/digital-paper.pdf");
    let ingested = harness
        .ingest(image, &fixture, fixture::MODEL, fixture::EFFORT)
        .await;
    let (envelope, expected) = fixture::note(&ingested.document);
    let fake = fixture::write_qoder(&harness.root, &envelope);
    let qoder_client = RunnerClient::new(&harness.base_url(), ingested.qoder_token.clone())
        .expect("Qoder Runner client");
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
            proxy_url: Some(ProxyUrl::parse("http://proxy.invalid:10809").expect("test proxy")),
        },
    );
    wait_for_job(
        &harness.pool,
        ingested.job_id,
        &daemon,
        Duration::from_secs(30),
    )
    .await;
    daemon.stop().await;

    let receipt = assertions::verify_and_write_receipt(&VerifyContext {
        pool: &harness.pool,
        sqlite_path: &harness.sqlite_path,
        artifact_root: &harness.artifact_root,
        address: harness.address,
        source_id: ingested.source_id,
        job_id: ingested.job_id,
        mode: assertions::VerifyMode::Fake {
            expected: &expected,
            captured_prompt: &fake.captured_prompt,
        },
        media_log: &ingested.media_log,
        secrets: [&ingested.media_token, &ingested.qoder_token],
    })
    .await;
    println!("FLORI_PDF_PRODUCT_RECEIPT={receipt}");
    harness.preserve();
}

pub(super) async fn run_real(config: RealConfig) {
    assert!(
        config.root.is_absolute(),
        "real receipt root must be absolute"
    );
    assert!(
        config.root.is_dir(),
        "real receipt root must already exist as a directory"
    );
    let receipt_dir = config.root.join("receipts");
    assert!(
        receipt_dir.is_dir(),
        "real receipt root must contain a receipts directory"
    );
    let run_root = config.root.join("run");
    assert!(
        !run_root.exists(),
        "real receipt run directory must not already exist"
    );
    let mut harness = Harness::new(
        run_root,
        true,
        fixture::REAL_PROMPT,
        &config.model,
        &config.effort,
    )
    .await;
    let ingested = harness
        .ingest(&config.image, &config.pdf, &config.model, &config.effort)
        .await;
    let runtime = PrivateQoderRuntime::new();
    let client = RunnerClient::new(&harness.base_url(), ingested.qoder_token.clone())
        .expect("Qoder Runner client");
    let daemon = AiDaemon::start(
        client,
        DaemonConfig {
            tool: AiTool::QoderCli,
            executable: config.executable,
            home: runtime.home(),
            tool_config_home: config.config_home,
            work_root: runtime.work(),
            model: config.model,
            effort: config.effort,
            renew_interval: Duration::from_secs(5),
            max_output_bytes: 1024 * 1024,
            proxy_url: Some(config.proxy_url),
        },
    );
    wait_for_job(
        &harness.pool,
        ingested.job_id,
        &daemon,
        Duration::from_secs(1_200),
    )
    .await;
    daemon.stop().await;
    drop(runtime);
    assert!(!harness.root.join("qoder-home").exists());
    assert!(!harness.root.join("qoder-work").exists());
    let receipt = assertions::verify_and_write_receipt(&VerifyContext {
        pool: &harness.pool,
        sqlite_path: &harness.sqlite_path,
        artifact_root: &harness.artifact_root,
        address: harness.address,
        source_id: ingested.source_id,
        job_id: ingested.job_id,
        mode: assertions::VerifyMode::Real,
        media_log: &ingested.media_log,
        secrets: [&ingested.media_token, &ingested.qoder_token],
    })
    .await;
    let receipt_copy = receipt_dir.join(format!("{}.json", ingested.job_id));
    assert!(
        !receipt_copy.exists(),
        "archived real receipt must not already exist"
    );
    fs::copy(harness.root.join("receipt.json"), &receipt_copy)
        .expect("archive real product receipt");
    println!("FLORI_PDF_PRODUCT_REAL_RECEIPT={receipt}");
    println!(
        "FLORI_PDF_PRODUCT_REAL_RECEIPT_COPY={}",
        receipt_copy.display()
    );
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

async fn wait_for_failed_job(pool: &SqlitePool, job_id: JobId, media: &mut DockerRunner) {
    tokio::time::timeout(Duration::from_secs(120), async {
        loop {
            let state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
                .bind(job_id.to_string())
                .fetch_one(pool)
                .await
                .expect("scanned PDF Job state");
            match state.as_str() {
                "failed" => break,
                "succeeded" | "canceled" => {
                    panic!("scanned PDF Job unexpectedly ended as {state}")
                }
                _ => media.assert_running(),
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    })
    .await
    .expect("runner-media did not reject the scanned PDF in 120 seconds");
}

async fn wait_for_job(pool: &SqlitePool, job_id: JobId, daemon: &AiDaemon, timeout: Duration) {
    tokio::time::timeout(timeout, async {
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
