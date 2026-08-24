use std::{
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{AttemptState, ErrorCode, FailAttemptRequest, TaskClaim};
use tokio::{fs, sync::watch};

use crate::{RunnerClient, manifest_sha256, task_log};

use super::{DownloadDaemonConfig, claim, execute};

pub(super) async fn run(
    client: &RunnerClient,
    config: &DownloadDaemonConfig,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    validate_config(config)?;
    fs::create_dir_all(&config.work_root)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    loop {
        let claim = tokio::select! {
            biased;
            () = canceled(cancel) => return Ok(()),
            result = client.poll() => result.map_err(|error| error.code())?,
        };
        match claim {
            Some(claim) => supervise(client, config, claim, cancel).await?,
            None => tokio::select! {
                biased;
                () = canceled(cancel) => return Ok(()),
                () = tokio::time::sleep(Duration::from_millis(250)) => {},
            },
        }
    }
}

fn validate_config(config: &DownloadDaemonConfig) -> Result<(), ErrorCode> {
    if !config.work_root.is_absolute()
        || !config.yt_dlp.is_absolute()
        || !config.yutto.is_absolute()
        || !config.output.ffprobe.is_absolute()
        || config.renew_interval.is_zero()
        || config.tool_timeout.is_zero()
        || config.max_tool_output_bytes == 0
        || config.output.max_files == 0
    {
        return Err(ErrorCode::InvalidRequest);
    }
    Ok(())
}

async fn supervise(
    client: &RunnerClient,
    config: &DownloadDaemonConfig,
    claim: TaskClaim,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let exec_id = claim.exec_id;
    let mut lease_deadline = deadline(claim.lease_expires_at_ms)?;
    let (stop, receiver) = watch::channel(false);
    let mut execution = Box::pin(execute_claim(client, config, claim, receiver));
    loop {
        tokio::select! {
            result = &mut execution => return result,
            result = async {
                tokio::time::sleep(config.renew_interval).await;
                client.renew(exec_id).await
            } => match result {
                Ok(renewed) => lease_deadline = deadline(renewed.lease_expires_at_ms)?,
                Err(error) => {
                    let _ = stop.send(true);
                    let _ = execution.await;
                    return Err(error.code());
                }
            },
            () = tokio::time::sleep_until(lease_deadline) => {
                let _ = stop.send(true);
                let _ = execution.await;
                return Err(ErrorCode::LeaseExpired);
            }
            () = canceled(cancel) => {
                let _ = stop.send(true);
                let _ = execution.await;
                return Err(ErrorCode::TaskCanceled);
            }
        }
    }
}

async fn execute_claim(
    client: &RunnerClient,
    config: &DownloadDaemonConfig,
    claim: TaskClaim,
    mut cancel: watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let source = match claim::validate(&claim) {
        Ok(source) => source.clone(),
        Err(code) => return fail(client, &claim, code).await,
    };
    let workspace = config.work_root.join(claim.exec_id.to_string());
    if fs::create_dir(&workspace).await.is_err() {
        return fail(client, &claim, ErrorCode::StorageUnavailable).await;
    }
    let result = execute_inner(client, config, &claim, &source, &workspace, &mut cancel).await;
    let cleanup = fs::remove_dir_all(&workspace).await;
    match (result, cleanup) {
        (Ok(()), Ok(())) => Ok(()),
        (Ok(()), Err(_)) => Err(ErrorCode::StorageUnavailable),
        (Err(code), _) => Err(code),
    }
}

async fn execute_inner(
    client: &RunnerClient,
    config: &DownloadDaemonConfig,
    claim: &TaskClaim,
    source: &flori_core::ResolvedSource,
    workspace: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    if let Err(code) = task_log::started(client, claim, "Download").await {
        return fail(client, claim, code).await;
    }
    let timeout = tokio::time::sleep(Duration::from_millis(claim.timeout_ms));
    tokio::pin!(timeout);
    let work = execute::run(client, config, claim, source, workspace, cancel);
    tokio::pin!(work);
    let result = tokio::select! {
        result = &mut work => result,
        () = &mut timeout => Err(ErrorCode::AttemptTimeout),
    };
    match result {
        Ok(entries) => {
            let digest = manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, entries)
                .map_err(|error| error.code())?;
            let ack = client
                .complete(claim.exec_id, digest)
                .await
                .map_err(|error| error.code())?;
            if ack.exec_id != claim.exec_id || ack.state != AttemptState::Succeeded {
                return Err(ErrorCode::CorruptState);
            }
            Ok(())
        }
        Err(code) => fail(client, claim, code).await,
    }
}

async fn fail(client: &RunnerClient, claim: &TaskClaim, code: ErrorCode) -> Result<(), ErrorCode> {
    let ack = client
        .fail(
            claim.exec_id,
            &FailAttemptRequest {
                error_code: code,
                manifest_sha256: None,
            },
        )
        .await
        .map_err(|error| error.code())?;
    if ack.exec_id != claim.exec_id || ack.state != AttemptState::Failed {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}

fn deadline(expires_at_ms: i64) -> Result<tokio::time::Instant, ErrorCode> {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ErrorCode::Internal)?
        .as_millis();
    let remaining = u128::try_from(expires_at_ms)
        .map_err(|_| ErrorCode::CorruptState)?
        .checked_sub(now_ms)
        .filter(|value| *value > 0)
        .ok_or(ErrorCode::LeaseExpired)?;
    tokio::time::Instant::now()
        .checked_add(Duration::from_millis(
            u64::try_from(remaining).map_err(|_| ErrorCode::CorruptState)?,
        ))
        .ok_or(ErrorCode::CorruptState)
}

async fn canceled(cancel: &mut watch::Receiver<bool>) {
    loop {
        if *cancel.borrow() || cancel.changed().await.is_err() {
            return;
        }
    }
}
