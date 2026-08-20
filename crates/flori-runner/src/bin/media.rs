#![forbid(unsafe_code)]

#[allow(dead_code)]
#[path = "../runtime_config.rs"]
mod runtime_config;

use std::{env, io, process::ExitCode, time::Duration};

use flori_core::ErrorCode;
use flori_runner::{PdfAcquireConfig, PdfDaemonConfig, PdfExtractConfig, run_pdf_daemon};
use tokio::sync::watch;

const RENEW_INTERVAL: Duration = Duration::from_secs(20);
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;
const PDF_MAX_BYTES: u64 = 128 * 1024 * 1024;

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("media Runner failed: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = ["run".into(), "media".into()];
    let runtime = runtime_config::parse_media(&args, |name| env::var_os(name))?;
    let config = PdfDaemonConfig {
        work_root: runtime.spool_dir.join("pdf-work"),
        acquire: PdfAcquireConfig {
            pdfinfo: "/usr/bin/pdfinfo".into(),
            pdftotext: "/usr/bin/pdftotext".into(),
            max_bytes: PDF_MAX_BYTES,
            max_probe_output_bytes: MAX_OUTPUT_BYTES,
            timeout: Duration::from_secs(10 * 60),
        },
        extract: PdfExtractConfig {
            python: "/usr/bin/python3".into(),
            timeout: Duration::from_secs(20 * 60),
            max_structure_bytes: 50 * 1024 * 1024,
            max_asset_bytes: 20 * 1024 * 1024,
            max_assets: 128,
        },
        renew_interval: RENEW_INTERVAL,
    };
    let (stop, mut cancel) = watch::channel(false);
    let mut daemon = Box::pin(run_pdf_daemon(&runtime.client, &config, &mut cancel));
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
