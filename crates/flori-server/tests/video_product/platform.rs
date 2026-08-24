use std::{
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
};

use flori_core::{
    CreateRemoteSource, DomainId, JobId, RegisterRunnerRequest, RunnerId, RunnerTool,
    RunnerToolCapability, SourceKind,
};
use flori_runner::{DownloadDaemonConfig, RunnerClient, run_download_daemon};
use sqlx::SqlitePool;
use tokio::{sync::watch, task::JoinHandle};

use super::{fixture, http};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Case {
    Local,
    Youtube,
    Bilibili,
}

pub(super) async fn create_source(
    address: SocketAddr,
    domain_id: DomainId,
    case: Case,
) -> flori_core::CreatedSource {
    let (kind, reference) = match case {
        Case::Youtube => (SourceKind::YoutubeVideo, "https://youtu.be/dQw4w9WgXcQ"),
        Case::Bilibili => (
            SourceKind::BilibiliVideo,
            "https://www.bilibili.com/video/BV1GJ411x7h7",
        ),
        Case::Local => unreachable!(),
    };
    http::create_remote(
        address,
        &CreateRemoteSource {
            request_key: format!("video-{case:?}"),
            kind,
            canonical_ref: reference.into(),
            title: Some(format!("{case:?} video golden")),
            domain_id,
            collection_ids: vec![],
            credential_id: None,
        },
    )
    .await
}

pub(super) struct RunningDownload {
    pub(super) stop: watch::Sender<bool>,
    pub(super) task: JoinHandle<Result<(), flori_core::ErrorCode>>,
}

pub(super) async fn start(
    address: SocketAddr,
    runner_id: RunnerId,
    root: &Path,
    ffprobe: &Path,
    case: Case,
) -> RunningDownload {
    let registration = RunnerClient::register(
        &format!("http://{address}"),
        fixture::DOWNLOAD_KEY,
        &capabilities(),
    )
    .await
    .expect("register download");
    assert_eq!(registration.runner_id, runner_id);
    let client = RunnerClient::new(&format!("http://{address}"), registration.token)
        .expect("download client");
    let (yt_dlp, yutto) = tools(root, case);
    let config = DownloadDaemonConfig::new(
        root.join("download-work"),
        yt_dlp,
        yutto,
        ffprobe.to_path_buf(),
        flori_runner::ProxyUrl::parse("http://youtube-proxy.invalid:1080").expect("proxy"),
    );
    let (stop, mut cancel) = watch::channel(false);
    let task =
        tokio::spawn(async move { run_download_daemon(&client, &config, &mut cancel).await });
    RunningDownload { stop, task }
}

fn capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: [RunnerTool::Ffprobe, RunnerTool::YtDlp, RunnerTool::Yutto]
            .into_iter()
            .map(|tool| RunnerToolCapability {
                tool,
                version: "fixture".into(),
            })
            .collect(),
        ai_models: vec![],
    }
}

fn tools(root: &Path, case: Case) -> (PathBuf, PathBuf) {
    let video =
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/vnext/local-video.mp4");
    let yt = if case == Case::Youtube {
        format!(
            "cp '{}' quarantine/download.mp4\nprintf '1\n00:00:00,000 --> 00:00:01,500\nHello video\n' > quarantine/download.en.srt\n",
            video.display()
        )
    } else {
        "exit 99\n".into()
    };
    let bili = if case == Case::Bilibili {
        format!(
            "cp '{}' quarantine/download.mp4\nprintf '<i><d p=\"0,1,25,0,0,0,0,0\">hello</d></i>' > quarantine/danmaku.xml\n",
            video.display()
        )
    } else {
        "exit 99\n".into()
    };
    (
        fixture::script(root, "yt-dlp", &yt),
        fixture::script(root, "yutto", &bili),
    )
}

pub(super) async fn assert_transcription(pool: &SqlitePool, job: JobId, root: &Path, case: Case) {
    let subtitle: i64 =
        sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE job_id=? AND kind='subtitle'")
            .bind(job.to_string())
            .fetch_one(pool)
            .await
            .expect("subtitle count");
    let danmaku: i64 =
        sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE job_id=? AND kind='danmaku'")
            .bind(job.to_string())
            .fetch_one(pool)
            .await
            .expect("danmaku count");
    assert_eq!(
        (subtitle, danmaku),
        match case {
            Case::Youtube => (1, 0),
            Case::Bilibili => (0, 1),
            Case::Local => (0, 0),
        }
    );
    let calls = fs::read(root.join("whisper-called")).map_or(0, |bytes| bytes.len());
    assert_eq!(calls, usize::from(case != Case::Youtube));
}
