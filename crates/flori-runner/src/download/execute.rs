use std::{os::unix::fs::PermissionsExt, path::Path};

use flori_core::{ArtifactManifestEntry, ErrorCode, ResolvedSource, SourceKind, TaskClaim};
use tokio::{fs, io::AsyncWriteExt, sync::watch};

use crate::{
    RunnerClient,
    child_process::{ChildProcessConfig, ChildTermination, run_child_process},
};

use super::{DownloadDaemonConfig, adapters, output};

pub(super) async fn run(
    client: &RunnerClient,
    config: &DownloadDaemonConfig,
    claim: &TaskClaim,
    source: &ResolvedSource,
    workspace: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let quarantine = workspace.join("quarantine");
    fs::create_dir(&quarantine)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    match source.kind {
        SourceKind::LocalVideo => {
            let input = source.input.as_ref().ok_or(ErrorCode::CorruptState)?;
            client
                .download_source_input(input, &quarantine.join("download.mp4"))
                .await
                .map_err(|error| error.code())?;
        }
        SourceKind::YoutubeVideo | SourceKind::BilibiliVideo => {
            run_platform(config, claim, source, workspace, &quarantine, cancel).await?;
        }
        _ => return Err(ErrorCode::UnsupportedSource),
    }
    output::publish(
        client,
        claim,
        &source.canonical_ref,
        &quarantine,
        workspace,
        &config.output,
        cancel,
    )
    .await
}

async fn run_platform(
    config: &DownloadDaemonConfig,
    claim: &TaskClaim,
    source: &ResolvedSource,
    workspace: &Path,
    quarantine: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let private = workspace.join("private");
    fs::create_dir(&private)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    fs::set_permissions(&private, std::fs::Permissions::from_mode(0o700))
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let credential = claim.secret_inputs.credential.as_ref();
    if credential.is_some_and(|value| !adapters::credential_matches(source.kind, value)) {
        return Err(ErrorCode::CredentialUnavailable);
    }
    let cookie = if let Some(credential) = credential {
        let path = private.join("cookie");
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        file.write_all(credential.value.as_bytes())
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        file.sync_all()
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        Some(path)
    } else {
        None
    };
    let temporary = workspace.join("temporary");
    fs::create_dir(&temporary)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let (executable, invocation) = match source.kind {
        SourceKind::YoutubeVideo => (
            &config.yt_dlp,
            adapters::youtube(
                &source.canonical_ref,
                quarantine,
                &config.youtube_proxy_url,
                cookie.as_deref(),
                &private,
            )?,
        ),
        SourceKind::BilibiliVideo => (
            &config.yutto,
            adapters::bilibili(
                &source.canonical_ref,
                quarantine,
                &temporary,
                cookie.as_deref(),
                &private,
            )?,
        ),
        _ => return Err(ErrorCode::UnsupportedSource),
    };
    let result = run_child_process(
        &ChildProcessConfig {
            executable: executable.clone(),
            arguments: invocation.arguments,
            working_directory: workspace.to_path_buf(),
            environment: invocation.environment,
            stdin: None,
            timeout: config.tool_timeout,
            max_output_bytes: config.max_tool_output_bytes,
        },
        cancel,
    )
    .await;
    let cleanup = fs::remove_dir_all(&private).await;
    let output = result?;
    cleanup.map_err(|_| ErrorCode::StorageUnavailable)?;
    drop(output.stderr);
    match (output.termination, output.exit_code) {
        (ChildTermination::Exited, Some(0)) => Ok(()),
        (ChildTermination::TimedOut, _) => Err(ErrorCode::AttemptTimeout),
        (ChildTermination::Canceled, _) => Err(ErrorCode::TaskCanceled),
        (ChildTermination::Exited, _) => Err(ErrorCode::ExecutorFailed),
    }
}
