use std::{env, io, path::PathBuf, process::ExitCode, time::Duration};

use flori_core::{AiTool, ErrorCode};
use flori_runner::{DaemonConfig, run_ai_daemon};
use tokio::sync::watch;

use crate::runtime_config;

const RENEW_INTERVAL: Duration = Duration::from_secs(20);
const MAX_OUTPUT_BYTES: usize = 1024 * 1024;

pub(crate) async fn run(tool: AiTool, executable: &'static str) -> ExitCode {
    match run_inner(tool, executable).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("AI Runner failed: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn run_inner(
    tool: AiTool,
    executable: &'static str,
) -> Result<(), Box<dyn std::error::Error>> {
    let args = match tool {
        AiTool::QoderCli => ["run", "qoder"],
        AiTool::CodexCli => ["run", "codex"],
    };
    let args = args.map(Into::into);
    let runtime = runtime_config::parse(&args, |name| env::var_os(name))?;
    let config = DaemonConfig {
        tool,
        executable: PathBuf::from(executable),
        home: runtime.home_dir,
        tool_config_home: runtime.tool_config_dir,
        work_root: runtime.spool_dir.join("work"),
        model: runtime.model,
        effort: runtime.effort,
        renew_interval: RENEW_INTERVAL,
        max_output_bytes: MAX_OUTPUT_BYTES,
        proxy_url: runtime.proxy_url,
    };
    let (stop, mut cancel) = watch::channel(false);
    let mut daemon = Box::pin(run_ai_daemon(&runtime.client, &config, &mut cancel));
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
