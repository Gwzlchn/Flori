use std::ffi::OsString;
use std::path::Path;
use std::process::Stdio;
use std::time::Duration;

use flori_core::ErrorCode;
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::process::Command;

pub(super) struct ProcessOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

pub(super) async fn run_bounded(
    program: &Path,
    arguments: &[OsString],
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<ProcessOutput, ErrorCode> {
    if !program.is_absolute() || timeout.is_zero() || max_output_bytes == 0 {
        return Err(ErrorCode::InvalidRequest);
    }
    let mut command = Command::new(program);
    command
        .args(arguments)
        .env_clear()
        .env("LANG", "C.UTF-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("PYTHONHASHSEED", "0")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn().map_err(spawn_error)?;
    let stdout = child.stdout.take().ok_or(ErrorCode::Internal)?;
    let stderr = child.stderr.take().ok_or(ErrorCode::Internal)?;
    let stdout = tokio::spawn(read_capped(stdout, max_output_bytes));
    let stderr = tokio::spawn(read_capped(stderr, max_output_bytes));
    let status = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(result) => result.map_err(|_| ErrorCode::ExecutorFailed)?,
        Err(_) => {
            child.kill().await.map_err(|_| ErrorCode::ExecutorFailed)?;
            let _ = stdout.await;
            let _ = stderr.await;
            return Err(ErrorCode::AttemptTimeout);
        }
    };
    let stdout = stdout.await.map_err(|_| ErrorCode::Internal)??;
    let stderr = stderr.await.map_err(|_| ErrorCode::Internal)??;
    if !status.success() {
        return Err(ErrorCode::ExecutorFailed);
    }
    let total = stdout
        .len()
        .checked_add(stderr.len())
        .filter(|size| *size <= max_output_bytes)
        .ok_or(ErrorCode::ArtifactTooLarge)?;
    debug_assert_eq!(total, stdout.len() + stderr.len());
    Ok(ProcessOutput { stdout, stderr })
}

async fn read_capped(
    mut reader: impl AsyncRead + Unpin,
    max_bytes: usize,
) -> Result<Vec<u8>, ErrorCode> {
    let mut result = Vec::new();
    let mut buffer = [0_u8; 8192];
    let mut exceeded = false;
    loop {
        let count = reader
            .read(&mut buffer)
            .await
            .map_err(|_| ErrorCode::ExecutorFailed)?;
        if count == 0 {
            break;
        }
        if result.len().saturating_add(count) <= max_bytes {
            result.extend_from_slice(&buffer[..count]);
        } else {
            exceeded = true;
        }
    }
    if exceeded {
        Err(ErrorCode::ArtifactTooLarge)
    } else {
        Ok(result)
    }
}

fn spawn_error(error: std::io::Error) -> ErrorCode {
    if error.kind() == std::io::ErrorKind::NotFound {
        ErrorCode::ToolTemporarilyUnavailable
    } else {
        ErrorCode::ExecutorFailed
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use flori_core::ArtifactId;

    use super::*;

    fn script(body: &str) -> (std::path::PathBuf, std::path::PathBuf) {
        let root = std::env::temp_dir().join(format!("flori-pdf-{}", ArtifactId::generate()));
        std::fs::create_dir(&root).expect("create test directory");
        let path = root.join("tool");
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write fake tool");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))
            .expect("make fake tool executable");
        (root, path)
    }

    #[tokio::test]
    async fn bounds_output_and_runtime() {
        let (root, tool) = script("printf 123456789");
        let oversized = run_bounded(&tool, &[], Duration::from_secs(1), 4).await;
        assert_eq!(oversized.err(), Some(ErrorCode::ArtifactTooLarge));
        std::fs::remove_dir_all(root).expect("remove test directory");

        let (root, tool) = script("while true; do :; done");
        let timed_out = run_bounded(&tool, &[], Duration::from_millis(20), 32).await;
        assert_eq!(timed_out.err(), Some(ErrorCode::AttemptTimeout));
        std::fs::remove_dir_all(root).expect("remove test directory");
    }
}
