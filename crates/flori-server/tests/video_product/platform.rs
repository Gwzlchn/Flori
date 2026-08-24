use std::{
    fs,
    net::SocketAddr,
    path::{Path, PathBuf},
};

use flori_core::{
    CreateRemoteSource, CredentialId, DomainId, JobId, RegisterRunnerRequest, RunnerId, RunnerTool,
    RunnerToolCapability, SourceKind,
};
use flori_runner::{DownloadDaemonConfig, RunnerClient, run_download_daemon};
use sqlx::SqlitePool;
use tokio::{sync::watch, task::JoinHandle};

use super::{fixture, http, platform_container};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Case {
    Local,
    Youtube,
    Bilibili,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Mode {
    Fake,
    External,
}

pub(super) async fn create_source(
    address: SocketAddr,
    pool: &SqlitePool,
    domain_id: DomainId,
    case: Case,
    mode: Mode,
) -> flori_core::CreatedSource {
    let (kind, reference, credential_kind, credential_value) = match case {
        Case::Youtube => (
            SourceKind::YoutubeVideo,
            "https://youtu.be/dQw4w9WgXcQ",
            "youtube_cookie",
            "fixture-cookie-youtube",
        ),
        Case::Bilibili => (
            SourceKind::BilibiliVideo,
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            "bilibili_cookie",
            "fixture-cookie-bilibili",
        ),
        Case::Local => unreachable!(),
    };
    let credential_id = if mode == Mode::Fake {
        let credential_id = CredentialId::generate();
        sqlx::query(
            "INSERT INTO credentials(id,kind,name,plaintext_value,created_at_ms,updated_at_ms) VALUES(?,?,?,?,0,0)",
        )
        .bind(credential_id.to_string())
        .bind(credential_kind)
        .bind(format!("fixture-{case:?}"))
        .bind(credential_value)
        .execute(pool)
        .await
        .expect("credential");
        Some(credential_id)
    } else {
        None
    };
    http::create_remote(
        address,
        &CreateRemoteSource {
            request_key: format!("video-{case:?}-{mode:?}"),
            kind,
            canonical_ref: reference.into(),
            title: Some(format!("{case:?} video golden")),
            domain_id,
            collection_ids: vec![],
            credential_id,
        },
    )
    .await
}

pub(super) enum RunningDownload {
    Fake {
        stop: watch::Sender<bool>,
        task: JoinHandle<Result<(), flori_core::ErrorCode>>,
    },
    External(platform_container::RunningContainer),
}

impl RunningDownload {
    pub(super) async fn stop(self) {
        match self {
            Self::Fake { stop, task } => {
                let _ = stop.send(true);
                let result = task.await.expect("download join");
                assert!(result.is_ok(), "download daemon: {result:?}");
            }
            Self::External(container) => container.stop().await,
        }
    }
}

pub(super) async fn start(
    address: SocketAddr,
    runner_id: RunnerId,
    root: &Path,
    ffprobe: &Path,
    case: Case,
    mode: Mode,
) -> RunningDownload {
    let registration = RunnerClient::register(
        &format!("http://{address}"),
        fixture::DOWNLOAD_KEY,
        &capabilities(mode),
    )
    .await
    .expect("register download");
    assert_eq!(registration.runner_id, runner_id);
    if mode == Mode::External {
        return RunningDownload::External(
            platform_container::start(address, registration.token).await,
        );
    }
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
    RunningDownload::Fake { stop, task }
}

fn capabilities(mode: Mode) -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: [RunnerTool::Ffprobe, RunnerTool::YtDlp, RunnerTool::Yutto]
            .into_iter()
            .map(|tool| RunnerToolCapability {
                version: match (mode, tool) {
                    (Mode::External, RunnerTool::Ffprobe) => "5.1.9",
                    (Mode::External, RunnerTool::YtDlp) => "2026.08.19",
                    (Mode::External, RunnerTool::Yutto) => "2.3.0",
                    (Mode::External, _) => unreachable!(),
                    (Mode::Fake, _) => "fixture",
                }
                .into(),
                tool,
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
            "case \"$*\" in *'--proxy http://youtube-proxy.invalid:1080/'*) ;; *) exit 91;; esac\nwhile [ \"$1\" != '--cookies' ]; do shift; done\nshift\ntest \"$(stat -c %a \"$1\")\" = 600\ntest \"$(cat \"$1\")\" = fixture-cookie-youtube\ntouch '{}'\ncp '{}' quarantine/download.mp4\nprintf '1\n00:00:00,000 --> 00:00:01,500\nHello video\n' > quarantine/download.en.srt\n",
            root.join("cookie-observed-youtube").display(),
            video.display(),
        )
    } else {
        "exit 99\n".into()
    };
    let bili = if case == Case::Bilibili {
        format!(
            "case \"$*\" in *'--proxy no'*) ;; *) exit 92;; esac\nwhile [ \"$1\" != '--auth-file' ]; do shift; done\nshift\ntest \"$(stat -c %a \"$1\")\" = 600\ntest \"$(cat \"$1\")\" = fixture-cookie-bilibili\ntouch '{}'\ncp '{}' quarantine/download.mp4\nprintf '<i><d p=\"0,1,25,0,0,0,0,0\">hello</d></i>' > quarantine/danmaku.xml\n",
            root.join("cookie-observed-bilibili").display(),
            video.display(),
        )
    } else {
        "exit 99\n".into()
    };
    (
        fixture::script(root, "yt-dlp", &yt),
        fixture::script(root, "yutto", &bili),
    )
}

pub(super) async fn assert_transcription(
    pool: &SqlitePool,
    job: JobId,
    root: &Path,
    case: Case,
    mode: Mode,
) {
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
    if case != Case::Local && mode == Mode::Fake {
        assert!(
            root.join(format!("cookie-observed-{}", case_name(case)))
                .is_file()
        );
        let mut work = fs::read_dir(root.join("download-work")).expect("download work");
        assert!(work.next().is_none(), "private workspace must be removed");
        let marker = format!("fixture-cookie-{}", case_name(case));
        let paths: Vec<String> =
            sqlx::query_scalar("SELECT relative_path FROM artifacts WHERE job_id=?")
                .bind(job.to_string())
                .fetch_all(pool)
                .await
                .expect("artifact paths");
        for path in paths {
            let bytes = fs::read(root.join("artifacts").join(path)).expect("artifact bytes");
            assert!(
                !bytes
                    .windows(marker.len())
                    .any(|value| value == marker.as_bytes())
            );
        }
    }
}

fn case_name(case: Case) -> &'static str {
    match case {
        Case::Youtube => "youtube",
        Case::Bilibili => "bilibili",
        Case::Local => "local",
    }
}
