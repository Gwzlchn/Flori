mod input;
mod invoke;
mod log;
mod output;

use std::{path::PathBuf, time::Duration};

use flori_core::{AiTool, ErrorCode, Executor, TaskClaim, UsageUpdate};
use tokio::{fs, sync::watch};

use crate::RunnerClient;

const INVOCATION_KEY: &str = "primary";

pub struct DaemonConfig {
    pub tool: AiTool,
    pub executable: PathBuf,
    pub home: PathBuf,
    pub tool_config_home: PathBuf,
    pub work_root: PathBuf,
    pub renew_interval: Duration,
    pub max_output_bytes: usize,
}

pub async fn run(
    client: &RunnerClient,
    config: &DaemonConfig,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    validate(config)?;
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

pub async fn run_claim(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: TaskClaim,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    validate(config)?;
    fs::create_dir_all(&config.work_root)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    supervise(client, config, claim, cancel).await
}

async fn supervise(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: TaskClaim,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let exec_id = claim.exec_id;
    let (stop, receiver) = watch::channel(false);
    let mut execution = Box::pin(execute(client, config, claim, receiver));
    loop {
        tokio::select! {
            result = &mut execution => return result,
            result = async {
                tokio::time::sleep(config.renew_interval).await;
                client.renew(exec_id).await
            } => {
                if let Err(error) = result {
                    let _ = stop.send(true);
                    let _ = execution.await;
                    return Err(error.code());
                }
            }
            () = canceled(cancel) => {
                let _ = stop.send(true);
                let result = execution.await;
                return result.and(Err(ErrorCode::TaskCanceled));
            }
        }
    }
}

async fn execute(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: TaskClaim,
    mut cancel: watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let workspace = config.work_root.join(claim.exec_id.to_string());
    let result = execute_inner(client, config, &claim, workspace.clone(), &mut cancel).await;
    match fs::remove_dir_all(&workspace).await {
        Ok(()) => result,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => result,
        Err(_) if result.is_ok() => Err(ErrorCode::StorageUnavailable),
        Err(_) => result,
    }
}

async fn execute_inner(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: &TaskClaim,
    workspace: PathBuf,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    validate_claim(claim)?;
    log::started(client, claim).await?;
    let prepared = match input::prepare(
        client,
        claim.executor,
        &claim.prompt_snapshot_sha256,
        &claim.resolved_inputs,
        workspace,
    )
    .await
    {
        Ok(prepared) => prepared,
        Err(error) => {
            let outcome = invoke::not_invoked(error)?;
            output::failure(
                client,
                claim,
                config.tool,
                INVOCATION_KEY,
                false,
                &outcome,
                error,
            )
            .await?;
            return Err(error);
        }
    };
    let model = claim.model.as_deref().ok_or(ErrorCode::CorruptState)?;
    let effort = claim.effort.as_deref().ok_or(ErrorCode::CorruptState)?;
    let started = UsageUpdate::Started {
        invocation_key: INVOCATION_KEY.to_owned(),
        tool: config.tool,
        model: model.to_owned(),
        effort: effort.to_owned(),
    };
    let ack = client
        .update_usage(claim.exec_id, &started)
        .await
        .map_err(|error| error.code())?;
    if !ack.applied {
        let outcome = invoke::not_invoked(ErrorCode::UsageConflict)?;
        output::failure(
            client,
            claim,
            config.tool,
            INVOCATION_KEY,
            true,
            &outcome,
            ErrorCode::UsageConflict,
        )
        .await?;
        return Err(ErrorCode::UsageConflict);
    }
    let outcome = match invoke::run(
        config,
        claim,
        INVOCATION_KEY,
        prepared.prompt,
        &prepared.workspace,
        cancel,
    )
    .await
    {
        Ok(outcome) => outcome,
        Err(error) => invoke::not_invoked(error)?,
    };
    if let Some(usage) = &outcome.usage
        && let Err(error) = client.update_usage(claim.exec_id, usage).await
    {
        let code = error.code();
        let _ = output::failure(
            client,
            claim,
            config.tool,
            INVOCATION_KEY,
            true,
            &outcome,
            code,
        )
        .await;
        return Err(code);
    }
    match outcome.result {
        Ok(_) => {
            match output::success(client, claim, config.tool, INVOCATION_KEY, &outcome).await {
                Ok(()) => Ok(()),
                Err(error) => {
                    let _ = output::failure(
                        client,
                        claim,
                        config.tool,
                        INVOCATION_KEY,
                        true,
                        &outcome,
                        error,
                    )
                    .await;
                    Err(error)
                }
            }
        }
        Err(error) => {
            output::failure(
                client,
                claim,
                config.tool,
                INVOCATION_KEY,
                true,
                &outcome,
                error,
            )
            .await?;
            Err(error)
        }
    }
}

fn validate(config: &DaemonConfig) -> Result<(), ErrorCode> {
    if !config.executable.is_absolute()
        || !config.home.is_absolute()
        || !config.tool_config_home.is_absolute()
        || !config.work_root.is_absolute()
        || config.renew_interval.is_zero()
        || config.max_output_bytes == 0
    {
        return Err(ErrorCode::InvalidRequest);
    }
    Ok(())
}

fn validate_claim(claim: &TaskClaim) -> Result<(), ErrorCode> {
    if !matches!(
        claim.executor,
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote
    ) || claim.model.is_none()
        || claim.effort.is_none()
        || claim.secret_inputs.credential.is_some()
        || claim.timeout_ms == 0
    {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}

async fn canceled(cancel: &mut watch::Receiver<bool>) {
    loop {
        if *cancel.borrow() || cancel.changed().await.is_err() {
            return;
        }
    }
}
