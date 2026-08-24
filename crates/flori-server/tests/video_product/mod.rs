mod fixture;
mod http;
mod qoder;

use std::{fs, os::unix::fs::PermissionsExt, sync::Arc, time::Duration};

use flori_core::{
    CreateJobRequest, CreateUploadSource, EvidenceLocator, EvidenceView, JobInputs, SearchHit,
    SourceKind, VideoKeyframe,
};
use flori_runner::{
    DaemonConfig, FasterWhisperConfig, PdfAcquireConfig, PdfDaemonConfig, PdfExtractConfig,
    RunnerClient, VideoDaemonConfig, run_ai_daemon, run_media_daemon,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{net::TcpListener, sync::watch};

pub(super) async fn run() {
    let root = std::env::temp_dir().join(format!(
        "flori-video-product-{}",
        flori_core::RequestId::generate()
    ));
    fs::create_dir(&root).expect("root");
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).expect("root mode");
    let database = root.join("flori.sqlite");
    let artifact_root = root.join("artifacts");
    let store = Arc::new(Store::open(&database).await.expect("store"));
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(&database)
            .foreign_keys(true),
    )
    .await
    .expect("pool");
    let artifacts =
        Arc::new(NasArtifactStore::new(&artifact_root, 128 * 1024 * 1024).expect("NAS"));
    let (domain, pipeline, media_id, ai_id) = fixture::seed(&store, &pool).await;
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let address = listener.local_addr().expect("address");
    let base = format!("http://{address}");
    let server_store = Arc::clone(&store);
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            flori_server::app(server_store, artifacts, base.clone(), 60_000).expect("app"),
        )
        .await
        .expect("server");
    });

    let video = include_bytes!("../../../../tests/fixtures/vnext/local-video.mp4");
    let uploaded = http::upload(
        address,
        &CreateUploadSource {
            request_key: "video-upload".into(),
            kind: SourceKind::LocalVideo,
            title: Some("Local video golden".into()),
            domain_id: domain,
            collection_ids: vec![],
            file_sha256: fixture::digest(video),
        },
        video,
    )
    .await;
    let job = http::create_job(
        address,
        &uploaded,
        &CreateJobRequest {
            request_key: "video-job".into(),
            pipeline_id: pipeline,
            inputs: JobInputs { translate: false },
        },
    )
    .await;

    let media_registration = RunnerClient::register(
        &format!("http://{address}"),
        fixture::MEDIA_KEY,
        &fixture::media_capabilities(),
    )
    .await
    .expect("register media");
    assert_eq!(media_registration.runner_id, media_id);
    let media = RunnerClient::new(&format!("http://{address}"), media_registration.token)
        .expect("media client");
    let tools = fixture::write_media_tools(&root);
    let media_config = VideoDaemonConfig {
        ffmpeg: tools.ffmpeg,
        ffprobe: tools.ffprobe,
        whisper: FasterWhisperConfig {
            python: tools.python,
            model_dir: tools.model,
            model_name: "base".into(),
            timeout: Duration::from_secs(5),
            max_output_bytes: 1024 * 1024,
        },
        tool_timeout: Duration::from_secs(5),
        max_tool_output_bytes: 1024 * 1024,
        requested_frames: 2,
        max_frame_bytes: 1024 * 1024,
    };
    let pdf_config = PdfDaemonConfig {
        work_root: root.join("media-work"),
        acquire: PdfAcquireConfig {
            pdfinfo: "/unused/pdfinfo".into(),
            pdftotext: "/unused/pdftotext".into(),
            max_bytes: 128 * 1024 * 1024,
            max_probe_output_bytes: 1024,
            timeout: Duration::from_secs(1),
        },
        extract: PdfExtractConfig {
            python: "/unused/python".into(),
            timeout: Duration::from_secs(1),
            max_structure_bytes: 1024,
            max_asset_bytes: 1024,
            max_assets: 1,
        },
        renew_interval: Duration::from_millis(100),
    };
    let (media_stop, mut media_cancel) = watch::channel(false);
    let media_task = tokio::spawn(async move {
        run_media_daemon(&media, &pdf_config, &media_config, &mut media_cancel).await
    });
    http::wait_task(&pool, job.job_id, "note", "ready").await;

    let transcript = sqlx::query(
        "SELECT a.relative_path FROM artifacts a WHERE a.job_id=? AND a.kind='transcript'",
    )
    .bind(job.job_id.to_string())
    .fetch_one(&pool)
    .await
    .expect("transcript");
    let bytes = fs::read(
        artifact_root.join(
            transcript
                .try_get::<String, _>("relative_path")
                .expect("path"),
        ),
    )
    .expect("transcript bytes");
    let transcript: flori_core::TranscriptManifest =
        serde_json::from_slice(&bytes).expect("strict transcript");
    let original: String =
        sqlx::query_scalar("SELECT id FROM artifacts WHERE job_id=? AND kind='source_original'")
            .bind(job.job_id.to_string())
            .fetch_one(&pool)
            .await
            .expect("source original");
    assert_eq!(transcript.source_artifact_id.to_string(), original);
    let frame = sqlx::query(
        "SELECT id,name FROM artifacts WHERE job_id=? AND kind='keyframe' ORDER BY name LIMIT 1",
    )
    .bind(job.job_id.to_string())
    .fetch_one(&pool)
    .await
    .expect("keyframe");
    let keyframe = VideoKeyframe::from_artifact_name(
        frame
            .try_get::<String, _>("id")
            .expect("frame ID")
            .parse()
            .expect("Artifact ID"),
        frame.try_get("name").expect("frame name"),
    )
    .expect("keyframe name");
    let repaired = fixture::envelope(transcript.source_artifact_id, keyframe);
    let qoder = qoder::invalid_then_repaired(&root, &repaired);
    let ai_registration = RunnerClient::register(
        &format!("http://{address}"),
        fixture::AI_KEY,
        &fixture::ai_capabilities(),
    )
    .await
    .expect("register AI");
    assert_eq!(ai_registration.runner_id, ai_id);
    let ai =
        RunnerClient::new(&format!("http://{address}"), ai_registration.token).expect("AI client");
    for child in ["ai-home", "ai-config", "ai-work"] {
        fs::create_dir(root.join(child)).expect("AI dir");
    }
    let ai_config = DaemonConfig {
        tool: flori_core::AiTool::QoderCli,
        executable: qoder.executable.clone(),
        home: root.join("ai-home"),
        tool_config_home: root.join("ai-config"),
        work_root: root.join("ai-work"),
        model: fixture::MODEL.into(),
        effort: fixture::EFFORT.into(),
        renew_interval: Duration::from_millis(100),
        max_output_bytes: 1024 * 1024,
        proxy_url: Some(
            flori_runner::ProxyUrl::parse("http://proxy.invalid:10809").expect("proxy"),
        ),
    };
    let (ai_stop, mut ai_cancel) = watch::channel(false);
    let ai_task = tokio::spawn(async move { run_ai_daemon(&ai, &ai_config, &mut ai_cancel).await });
    http::wait_published(&pool, job.job_id).await;
    qoder::assert_repaired(&pool, job.job_id, &qoder).await;

    let hits: Vec<SearchHit> = http::get_json(address, "/api/v1/search?q=Hello&limit=10").await;
    assert!(hits.len() >= 2);
    assert!(hits.iter().all(|hit| hit.job_id == job.job_id));
    let evidence_id = hits
        .iter()
        .find_map(|hit| hit.evidence_ids.first())
        .expect("FTS evidence");
    let evidence: EvidenceView =
        http::get_json(address, &format!("/api/v1/evidence/{evidence_id}")).await;
    assert_eq!(
        (evidence.source_id, evidence.job_id),
        (uploaded.source_id, job.job_id)
    );
    assert!(matches!(
        evidence.locator,
        EvidenceLocator::Video {
            start_ms: 0,
            end_ms: 1_500,
            keyframe: Some(_)
        }
    ));
    let current: Option<String> =
        sqlx::query_scalar("SELECT current_job_id FROM sources WHERE id=?")
            .bind(uploaded.source_id.to_string())
            .fetch_one(&pool)
            .await
            .expect("current");
    let expected_job = job.job_id.to_string();
    assert_eq!(current.as_deref(), Some(expected_job.as_str()));
    let artifacts: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE job_id=?")
        .bind(job.job_id.to_string())
        .fetch_one(&pool)
        .await
        .expect("artifacts");
    assert_eq!(artifacts, 15);

    let _ = media_stop.send(true);
    let _ = ai_stop.send(true);
    let media_result = media_task.await.expect("media join");
    assert!(media_result.is_ok(), "media daemon: {media_result:?}");
    let ai_result = ai_task.await.expect("AI join");
    assert!(ai_result.is_ok(), "AI daemon: {ai_result:?}");
    server.abort();
    pool.close().await;
    drop(store);
    fs::remove_dir_all(root).expect("cleanup");
}
