mod input;
mod invocation_flow;
mod invoke;
mod log;
mod output;

use std::{
    path::PathBuf,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::{AiTool, ErrorCode, Executor, TaskClaim};
use reqwest::Url;
use tokio::{fs, sync::watch};

use crate::RunnerClient;

pub struct DaemonConfig {
    pub tool: AiTool,
    pub executable: PathBuf,
    pub home: PathBuf,
    pub tool_config_home: PathBuf,
    pub work_root: PathBuf,
    pub model: String,
    pub effort: String,
    pub renew_interval: Duration,
    pub max_output_bytes: usize,
    pub proxy_url: Option<Url>,
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

async fn supervise(
    client: &RunnerClient,
    config: &DaemonConfig,
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
            } => {
                match result {
                    Ok(renewed) => match deadline(renewed.lease_expires_at_ms) {
                        Ok(renewed_deadline) => lease_deadline = renewed_deadline,
                        Err(code) => {
                            let _ = stop.send(true);
                            let _ = execution.await;
                            return Err(code);
                        }
                    },
                    Err(error) => {
                        let _ = stop.send(true);
                        let _ = execution.await;
                        return Err(error.code());
                    }
                }
            }
            () = tokio::time::sleep_until(lease_deadline) => {
                let _ = stop.send(true);
                let _ = execution.await;
                return Err(ErrorCode::LeaseExpired);
            }
            () = canceled(cancel) => {
                let _ = stop.send(true);
                let result = execution.await;
                return result.and(Err(ErrorCode::TaskCanceled));
            }
        }
    }
}

fn deadline(expires_at_ms: i64) -> Result<tokio::time::Instant, ErrorCode> {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| ErrorCode::Internal)?
        .as_millis();
    let expires = u128::try_from(expires_at_ms).map_err(|_| ErrorCode::CorruptState)?;
    let remaining = expires
        .checked_sub(now_ms)
        .filter(|remaining| *remaining > 0)
        .ok_or(ErrorCode::LeaseExpired)?;
    let millis = u64::try_from(remaining).map_err(|_| ErrorCode::CorruptState)?;
    tokio::time::Instant::now()
        .checked_add(Duration::from_millis(millis))
        .ok_or(ErrorCode::CorruptState)
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
    validate_claim(config, claim)?;
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
            output::failure(client, claim, config.tool, &[], &outcome, error).await?;
            return Ok(());
        }
    };
    invocation_flow::run(client, config, claim, prepared, cancel).await
}

fn validate(config: &DaemonConfig) -> Result<(), ErrorCode> {
    if !config.executable.is_absolute()
        || !config.home.is_absolute()
        || !config.tool_config_home.is_absolute()
        || !config.work_root.is_absolute()
        || !identifier(&config.model)
        || !identifier(&config.effort)
        || config.renew_interval.is_zero()
        || config.max_output_bytes == 0
        || !valid_proxy(config)
    {
        return Err(ErrorCode::InvalidRequest);
    }
    Ok(())
}

fn valid_proxy(config: &DaemonConfig) -> bool {
    match (&config.tool, &config.proxy_url) {
        #[cfg(feature = "qoder")]
        (AiTool::QoderCli, Some(url)) => {
            url.scheme() == "http"
                && url.host().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.path() == "/"
                && url.query().is_none()
                && url.fragment().is_none()
        }
        #[cfg(feature = "codex")]
        (AiTool::CodexCli, Some(url)) => {
            url.scheme() == "http"
                && url.host().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.path() == "/"
                && url.query().is_none()
                && url.fragment().is_none()
        }
        _ => false,
    }
}

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

fn validate_claim(config: &DaemonConfig, claim: &TaskClaim) -> Result<(), ErrorCode> {
    if !matches!(
        claim.executor,
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote
    ) || claim.model.as_deref() != Some(config.model.as_str())
        || claim.effort.as_deref() != Some(config.effort.as_str())
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
