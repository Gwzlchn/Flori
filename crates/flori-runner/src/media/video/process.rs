use std::{
    ffi::OsString,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::Command,
    sync::watch,
};

use super::VideoMediaError;

pub(super) struct ToolOutput {
    pub(super) stdout: Vec<u8>,
    pub(super) stderr: Vec<u8>,
}

pub(super) async fn run_tool(
    program: &Path,
    arguments: &[OsString],
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<u8>, VideoMediaError> {
    Ok(
        run_tool_output(program, arguments, timeout, max_output_bytes)
            .await?
            .stdout,
    )
}

pub(super) async fn run_tool_output(
    program: &Path,
    arguments: &[OsString],
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<ToolOutput, VideoMediaError> {
    if !program.is_absolute() || timeout.is_zero() || max_output_bytes == 0 {
        return Err(VideoMediaError::ToolFailed);
    }
    let mut command = Command::new(program);
    command
        .args(arguments)
        .env_clear()
        .env("HF_HUB_OFFLINE", "1")
        .env("LANG", "C.UTF-8")
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONHASHSEED", "0")
        .env("TRANSFORMERS_OFFLINE", "1")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn().map_err(|_| VideoMediaError::ToolFailed)?;
    let stdout = child.stdout.take().ok_or(VideoMediaError::ToolFailed)?;
    let stderr = child.stderr.take().ok_or(VideoMediaError::ToolFailed)?;
    let total = Arc::new(AtomicUsize::new(0));
    let (limit_tx, mut limit_rx) = watch::channel(false);
    let _limit_guard = limit_tx.clone();
    let stdout_task = tokio::spawn(read_bounded(
        stdout,
        max_output_bytes,
        total.clone(),
        limit_tx.clone(),
    ));
    let stderr_task = tokio::spawn(read_bounded(stderr, max_output_bytes, total, limit_tx));
    let status = tokio::select! {
        status = child.wait() => status.map_err(|_| VideoMediaError::ToolFailed)?,
        () = tokio::time::sleep(timeout) => {
            let _ = child.kill().await;
            return Err(VideoMediaError::ToolTimedOut);
        }
        result = limit_rx.changed() => {
            let _ = result;
            let _ = child.kill().await;
            return Err(VideoMediaError::OutputTooLarge);
        }
    };
    let stdout = stdout_task
        .await
        .map_err(|_| VideoMediaError::ToolFailed)??;
    let stderr = stderr_task
        .await
        .map_err(|_| VideoMediaError::ToolFailed)??;
    if !status.success() {
        return Err(VideoMediaError::ToolFailed);
    }
    Ok(ToolOutput { stdout, stderr })
}

async fn read_bounded(
    mut reader: impl AsyncRead + Unpin,
    max: usize,
    total: Arc<AtomicUsize>,
    limit: watch::Sender<bool>,
) -> Result<Vec<u8>, VideoMediaError> {
    let mut output = Vec::new();
    let mut buffer = [0_u8; 8_192];
    loop {
        let read = reader
            .read(&mut buffer)
            .await
            .map_err(|_| VideoMediaError::ToolFailed)?;
        if read == 0 {
            return Ok(output);
        }
        if total
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_add(read).filter(|next| *next <= max)
            })
            .is_err()
        {
            let _ = limit.send(true);
            return Err(VideoMediaError::OutputTooLarge);
        }
        output.extend_from_slice(&buffer[..read]);
    }
}
