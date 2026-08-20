use std::{
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, AttemptState, ErrorCode, FailAttemptRequest,
    ResolvedTaskInputs, TaskClaim,
};
use tokio::{fs, sync::watch};

use crate::{RunnerClient, manifest_sha256};

use super::{PdfAcquireConfig, PdfExtractConfig, acquire_pdf, claim, extract_pdf, log, upload};

pub struct PdfDaemonConfig {
    pub work_root: PathBuf,
    pub acquire: PdfAcquireConfig,
    pub extract: PdfExtractConfig,
    pub renew_interval: Duration,
}

pub async fn run_pdf_daemon(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
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
            result = client.poll() => result.map_err(|error| error.code())?,
            () = canceled(cancel) => return Ok(()),
        };
        match claim {
            Some(claim) => supervise(client, config, claim, cancel).await?,
            None => tokio::select! {
                () = tokio::time::sleep(Duration::from_millis(250)) => {},
                () = canceled(cancel) => return Ok(()),
            },
        }
    }
}

async fn supervise(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    claim: TaskClaim,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let exec_id = claim.exec_id;
    let mut lease_deadline = deadline(claim.lease_expires_at_ms)?;
    let (stop, receiver) = watch::channel(false);
    let mut execution = Box::pin(execute(client, config, claim, receiver));
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
    claim: TaskClaim,
    mut cancel: watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let workspace = config.work_root.join(claim.exec_id.to_string());
    let result = execute_inner(client, config, &claim, &workspace, &mut cancel).await;
    let cleanup = fs::remove_dir_all(&workspace).await;
    match (result, cleanup) {
        (result, Ok(())) => result,
        (result, Err(error)) if error.kind() == std::io::ErrorKind::NotFound => result,
        (Ok(()), Err(_)) => Err(ErrorCode::StorageUnavailable),
        (Err(code), Err(_)) => Err(code),
    }
}

async fn execute_inner(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    claim: &TaskClaim,
    workspace: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    if let Err(code) = claim::validate(claim) {
        return fail(client, claim, code).await;
    }
    if fs::create_dir(workspace).await.is_err() {
        return fail(client, claim, ErrorCode::StorageUnavailable).await;
    }
    if let Err(code) = log::started(client, claim).await {
        return fail(client, claim, code).await;
    }
    let timeout = tokio::time::sleep(Duration::from_millis(claim.timeout_ms));
    tokio::pin!(timeout);
    let work = run_task(client, config, claim, workspace);
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

async fn run_task(
    client: &RunnerClient,
    config: &PdfDaemonConfig,
    claim: &TaskClaim,
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    match &claim.resolved_inputs {
        ResolvedTaskInputs::DocumentAcquire { source } => {
            let path = workspace.join("source.pdf");
            acquire_pdf(client, source, &path, &config.acquire).await?;
            let declaration = claim::exact(claim, ArtifactKind::SourceOriginal)?;
            Ok(vec![
                upload::file(
                    client,
                    claim,
                    declaration,
                    declaration.name.clone(),
                    "application/pdf",
                    &path,
                )
                .await?,
            ])
        }
        ResolvedTaskInputs::DocumentExtract { pdf } => {
            let input = workspace.join("source.pdf");
            client
                .download_artifact(pdf, &input)
                .await
                .map_err(|error| error.code())?;
            let output = workspace.join("output");
            let structure = extract_pdf(pdf, &input, &output, &config.extract).await?;
            let mut entries =
                Vec::with_capacity(1 + structure.figures.len() + structure.tables.len());
            let declaration = claim::exact(claim, ArtifactKind::DocumentStructure)?;
            entries.push(
                upload::file(
                    client,
                    claim,
                    declaration,
                    declaration.name.clone(),
                    "application/json",
                    &output.join("document.json"),
                )
                .await?,
            );
            for (kind, name) in structure
                .figures
                .iter()
                .map(|item| (ArtifactKind::Figure, &item.artifact_name))
                .chain(
                    structure
                        .tables
                        .iter()
                        .map(|item| (ArtifactKind::TableRegion, &item.artifact_name)),
                )
            {
                let declaration = claim::exact(claim, kind)?;
                entries.push(
                    upload::file(
                        client,
                        claim,
                        declaration,
                        format!("{}/{}", declaration.name, claim::basename(name)?),
                        "image/png",
                        &output.join(name),
                    )
                    .await?,
                );
            }
            Ok(entries)
        }
        _ => Err(ErrorCode::CorruptState),
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
