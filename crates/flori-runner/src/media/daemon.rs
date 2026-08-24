use std::{
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{AttemptState, ErrorCode, Executor, FailAttemptRequest, TaskClaim};
use tokio::{fs, sync::watch};

use crate::{RunnerClient, manifest_sha256, task_log};

use super::{
    pdf::{claim, daemon::PdfDaemonConfig},
    video_claim,
    video_daemon::{self, VideoDaemonConfig},
};

pub(super) async fn run_pdf(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    run(client, config, None, cancel).await
}

pub(super) async fn run_media(
    client: &RunnerClient,
    pdf: &PdfDaemonConfig,
    video: &VideoDaemonConfig,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    run(client, pdf, Some(video), cancel).await
}

async fn run(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    video: Option<&VideoDaemonConfig>,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    if !config.work_root.is_absolute() || config.renew_interval.is_zero() {
        return Err(ErrorCode::InvalidRequest);
    }
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
            Some(claim) => supervise(client, config, video, claim, cancel).await?,
            None => tokio::select! {
                biased;
                () = canceled(cancel) => return Ok(()),
                () = tokio::time::sleep(Duration::from_millis(250)) => {},
            },
        }
    }
}

async fn supervise(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    video: Option<&VideoDaemonConfig>,
    claim: TaskClaim,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let exec_id = claim.exec_id;
    let mut lease_deadline = deadline(claim.lease_expires_at_ms)?;
    let (stop, receiver) = watch::channel(false);
    let mut execution = Box::pin(execute(client, config, video, claim, receiver));
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

async fn execute(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    video: Option<&VideoDaemonConfig>,
    claim: TaskClaim,
    mut cancel: watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let workspace = config.work_root.join(claim.exec_id.to_string());
    let result = execute_inner(client, config, video, &claim, &workspace, &mut cancel).await;
    match fs::remove_dir_all(&workspace).await {
        Ok(()) => result,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => result,
        Err(_) if result.is_ok() => Err(ErrorCode::StorageUnavailable),
        Err(_) => result,
    }
}

async fn execute_inner(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    video: Option<&VideoDaemonConfig>,
    claim: &TaskClaim,
    workspace: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let task_kind = match claim.executor {
        Executor::DocumentAcquire | Executor::DocumentExtract => {
            if let Err(code) = claim::validate(claim) {
                return fail(client, claim, code).await;
            }
            "PDF"
        }
        Executor::VideoTranscribe | Executor::VideoFrames | Executor::VideoMechanicalNote => {
            let Some(_) = video else {
                return fail(client, claim, ErrorCode::CorruptState).await;
            };
            if let Err(code) = video_claim::validate(claim) {
                return fail(client, claim, code).await;
            }
            "Video"
        }
        _ => return fail(client, claim, ErrorCode::CorruptState).await,
    };
    if fs::create_dir(workspace).await.is_err() {
        return fail(client, claim, ErrorCode::StorageUnavailable).await;
    }
    if let Err(code) = task_log::started(client, claim, task_kind).await {
        return fail(client, claim, code).await;
    }
    let timeout = tokio::time::sleep(Duration::from_millis(claim.timeout_ms));
    tokio::pin!(timeout);
    let work = async {
        match claim.executor {
            Executor::DocumentAcquire | Executor::DocumentExtract => {
                super::pdf::daemon::run_task(client, config, claim, workspace).await
            }
            Executor::VideoTranscribe | Executor::VideoFrames | Executor::VideoMechanicalNote => {
                video_daemon::run_task(
                    client,
                    video.ok_or(ErrorCode::CorruptState)?,
                    claim,
                    workspace,
                )
                .await
            }
            _ => Err(ErrorCode::CorruptState),
        }
    };
    tokio::pin!(work);
    let result = tokio::select! {
        result = &mut work => result,
        () = &mut timeout => Err(ErrorCode::AttemptTimeout),
        () = canceled(cancel) => Err(ErrorCode::TaskCanceled),
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

async fn fail(
    client: &RunnerClient,
    claim: &TaskClaim,
    error_code: ErrorCode,
) -> Result<(), ErrorCode> {
    let ack = client
        .fail(
            claim.exec_id,
            &FailAttemptRequest {
                error_code,
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
    if *cancel.borrow() {
        return;
    }
    let _ = cancel.changed().await;
}
