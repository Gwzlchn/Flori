use std::{path::Path, path::PathBuf, time::Duration};

use flori_core::{ArtifactKind, ArtifactManifestEntry, ErrorCode, ResolvedTaskInputs, TaskClaim};
use tokio::sync::watch;

use crate::{RunnerClient, task_upload};

use super::{PdfAcquireConfig, PdfExtractConfig, acquire_pdf, claim, extract_pdf, scholarly};

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
    crate::media::daemon::run_pdf(client, config, cancel).await
}

pub(crate) async fn run_task(
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
            let mut entries = vec![
                task_upload::file(
                    client,
                    claim,
                    declaration,
                    declaration.name.clone(),
                    "application/pdf",
                    &path,
                )
                .await?,
            ];
            entries.extend(
                scholarly::capture(
                    client,
                    claim,
                    source,
                    &workspace.join("scholarly"),
                    config.acquire.timeout,
                )
                .await?,
            );
            Ok(entries)
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
                task_upload::file(
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
                    task_upload::file(
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
