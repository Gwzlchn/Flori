use std::{path::Path, time::Duration};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ErrorCode, PartsManifest, PartsManifestSchema,
    ResolvedSource, TaskClaim, VideoPart,
};
use tokio::fs;

use crate::{RunnerClient, media::pdf};

use super::video::probe_video;

pub(crate) async fn acquire_local(
    client: &RunnerClient,
    ffprobe: &Path,
    timeout: Duration,
    max_output_bytes: usize,
    claim: &TaskClaim,
    source: &ResolvedSource,
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let input = source.input.as_ref().ok_or(ErrorCode::CorruptState)?;
    let output = workspace.join("output/videos");
    fs::create_dir_all(&output)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let video_path = output.join("part-1.mp4");
    client
        .download_source_input(input, &video_path)
        .await
        .map_err(|error| error.code())?;
    let probe = probe_video(ffprobe, &video_path, timeout, max_output_bytes)
        .await
        .map_err(|error| error.code())?;
    if probe.video_streams != 1 || probe.audio_streams == 0 {
        return Err(ErrorCode::ExecutorFailed);
    }
    let declaration = pdf::claim::exact(claim, ArtifactKind::SourceOriginal)?;
    let video = pdf::upload::file(
        client,
        claim,
        declaration,
        "videos/part-1.mp4".into(),
        "video/mp4",
        &video_path,
    )
    .await?;
    let parts = PartsManifest {
        schema: PartsManifestSchema::V1,
        parts: vec![VideoPart {
            index: 1,
            title: source.canonical_ref.clone(),
            duration_ms: probe.duration_ms,
            video_artifact_name: video.name.clone(),
            subtitle_artifact_name: None,
            danmaku_artifact_name: None,
        }],
    };
    parts.validate().map_err(|_| ErrorCode::CorruptState)?;
    let path = workspace.join("output/parts.json");
    fs::write(
        &path,
        serde_json::to_vec(&parts).map_err(|_| ErrorCode::Internal)?,
    )
    .await
    .map_err(|_| ErrorCode::StorageUnavailable)?;
    let declaration = pdf::claim::exact(claim, ArtifactKind::PartsManifest)?;
    let manifest = pdf::upload::file(
        client,
        claim,
        declaration,
        declaration.name.clone(),
        "application/json",
        &path,
    )
    .await?;
    Ok(vec![video, manifest])
}
