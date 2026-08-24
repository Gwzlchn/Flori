mod adapters;
mod claim;
mod daemon;
mod execute;
mod output;
mod probe;

use std::{path::PathBuf, time::Duration};

pub struct DownloadDaemonConfig {
    pub work_root: PathBuf,
    pub yt_dlp: PathBuf,
    pub yutto: PathBuf,
    pub youtube_proxy_url: reqwest::Url,
    pub renew_interval: Duration,
    pub tool_timeout: Duration,
    pub max_tool_output_bytes: usize,
    output: output::OutputLimits,
}

impl DownloadDaemonConfig {
    pub fn new(
        work_root: PathBuf,
        yt_dlp: PathBuf,
        yutto: PathBuf,
        ffprobe: PathBuf,
        youtube_proxy_url: reqwest::Url,
    ) -> Self {
        Self {
            work_root,
            yt_dlp,
            yutto,
            youtube_proxy_url,
            renew_interval: Duration::from_secs(20),
            tool_timeout: Duration::from_secs(30 * 60),
            max_tool_output_bytes: 1024 * 1024,
            output: output::OutputLimits {
                ffprobe,
                timeout: Duration::from_secs(10 * 60),
                max_tool_output_bytes: 1024 * 1024,
                max_files: 32,
            },
        }
    }
}

pub async fn run_download_daemon(
    client: &crate::RunnerClient,
    config: &DownloadDaemonConfig,
    cancel: &mut tokio::sync::watch::Receiver<bool>,
) -> Result<(), flori_core::ErrorCode> {
    daemon::run(client, config, cancel).await
}
