#![forbid(unsafe_code)]

#[allow(dead_code)]
#[path = "../runtime_config.rs"]
mod runtime_config;

use std::{env, io, process::ExitCode};

use flori_core::ErrorCode;
use flori_runner::{DownloadDaemonConfig, run_download_daemon};
use tokio::sync::watch;

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("download Runner failed: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = ["run".into(), "download".into()];
    let runtime = runtime_config::parse_download(&args, |name| env::var_os(name))?;
    let config = DownloadDaemonConfig::new(
        runtime.spool_dir.join("download-work"),
        "/usr/local/bin/yt-dlp".into(),
        "/usr/local/bin/yutto".into(),
        "/usr/bin/ffprobe".into(),
        runtime.youtube_proxy_url,
    );
    let (stop, mut cancel) = watch::channel(false);
    let mut daemon = Box::pin(run_download_daemon(&runtime.client, &config, &mut cancel));
    tokio::select! {
        result = &mut daemon => daemon_result(result),
        signal = tokio::signal::ctrl_c() => {
            signal?;
            let _ = stop.send(true);
            match daemon.await {
                Err(ErrorCode::TaskCanceled) | Ok(()) => Ok(()),
                Err(code) => daemon_result(Err(code)),
            }
        }
    }
}

fn daemon_result(result: Result<(), ErrorCode>) -> Result<(), Box<dyn std::error::Error>> {
    result.map_err(|code| io::Error::other(format!("daemon stopped: {code:?}")).into())
}
