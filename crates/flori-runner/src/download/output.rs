use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ErrorCode, PartsManifest, PartsManifestSchema, TaskClaim,
    VideoPart,
};
use tokio::{fs, sync::watch};

use crate::{RunnerClient, task_upload};

use super::{claim, probe::probe};

pub(super) struct OutputLimits {
    pub(super) ffprobe: PathBuf,
    pub(super) timeout: Duration,
    pub(super) max_tool_output_bytes: usize,
    pub(super) max_files: usize,
}

pub(super) async fn publish(
    client: &RunnerClient,
    claim: &TaskClaim,
    canonical_ref: &str,
    quarantine: &Path,
    workspace: &Path,
    limits: &OutputLimits,
    cancel: &mut watch::Receiver<bool>,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let discovered = discover(quarantine, limits.max_files).await?;
    let video = one(&discovered.videos)?;
    let subtitle = preferred_subtitle(&discovered.subtitles);
    let danmaku = optional_one(&discovered.danmaku)?;
    let output = workspace.join("output");
    fs::create_dir(&output)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let video_path = output.join("part-1.mp4");
    copy_regular(video, &video_path).await?;
    let probe = probe(
        &limits.ffprobe,
        &video_path,
        limits.timeout,
        limits.max_tool_output_bytes,
        cancel,
    )
    .await?;
    if probe.video_streams != 1 || probe.audio_streams == 0 || probe.duration_ms == 0 {
        return Err(ErrorCode::ExecutorFailed);
    }

    let video_declaration = claim::exact(claim, ArtifactKind::SourceOriginal)?;
    let video_entry = task_upload::file(
        client,
        claim,
        video_declaration,
        "videos/part-1.mp4".into(),
        "video/mp4",
        &video_path,
    )
    .await?;
    let mut entries = vec![video_entry.clone()];
    let subtitle_name = copy_and_upload(
        client,
        claim,
        subtitle,
        ArtifactKind::Subtitle,
        "subtitle.srt",
        "application/x-subrip",
        &output,
    )
    .await?;
    if let Some(entry) = subtitle_name.1 {
        entries.push(entry);
    }
    let danmaku_name = copy_and_upload(
        client,
        claim,
        danmaku,
        ArtifactKind::Danmaku,
        "danmaku.xml",
        "application/xml",
        &output,
    )
    .await?;
    if let Some(entry) = danmaku_name.1 {
        entries.push(entry);
    }
    let parts = PartsManifest {
        schema: PartsManifestSchema::V1,
        parts: vec![VideoPart {
            index: 1,
            title: canonical_ref.to_owned(),
            duration_ms: probe.duration_ms,
            video_artifact_name: video_entry.name,
            subtitle_artifact_name: subtitle_name.0,
            danmaku_artifact_name: danmaku_name.0,
        }],
    };
    parts.validate().map_err(|_| ErrorCode::CorruptState)?;
    let parts_path = output.join("parts.json");
    fs::write(
        &parts_path,
        serde_json::to_vec(&parts).map_err(|_| ErrorCode::Internal)?,
    )
    .await
    .map_err(|_| ErrorCode::StorageUnavailable)?;
    let declaration = claim::exact(claim, ArtifactKind::PartsManifest)?;
    entries.push(
        task_upload::file(
            client,
            claim,
            declaration,
            declaration.name.clone(),
            "application/json",
            &parts_path,
        )
        .await?,
    );
    Ok(entries)
}

async fn copy_and_upload(
    client: &RunnerClient,
    claim: &TaskClaim,
    source: Option<&PathBuf>,
    kind: ArtifactKind,
    name: &str,
    media_type: &str,
    output: &Path,
) -> Result<(Option<String>, Option<ArtifactManifestEntry>), ErrorCode> {
    let Some(source) = source else {
        return Ok((None, None));
    };
    let path = output.join(name);
    copy_regular(source, &path).await?;
    let declaration = claim::exact(claim, kind)?;
    let entry = task_upload::file(
        client,
        claim,
        declaration,
        declaration.name.clone(),
        media_type,
        &path,
    )
    .await?;
    Ok((Some(entry.name.clone()), Some(entry)))
}

struct Discovered {
    videos: Vec<PathBuf>,
    subtitles: Vec<PathBuf>,
    danmaku: Vec<PathBuf>,
}

async fn discover(root: &Path, max_files: usize) -> Result<Discovered, ErrorCode> {
    let mut pending = vec![(root.to_path_buf(), 0_u8)];
    let mut result = Discovered {
        videos: vec![],
        subtitles: vec![],
        danmaku: vec![],
    };
    let mut count = 0_usize;
    while let Some((directory, depth)) = pending.pop() {
        if depth > 3 {
            return Err(ErrorCode::ArtifactInvalidPath);
        }
        let mut entries = fs::read_dir(directory)
            .await
            .map_err(|_| ErrorCode::ExecutorFailed)?;
        while let Some(entry) = entries
            .next_entry()
            .await
            .map_err(|_| ErrorCode::ExecutorFailed)?
        {
            let path = entry.path();
            let metadata = fs::symlink_metadata(&path)
                .await
                .map_err(|_| ErrorCode::ExecutorFailed)?;
            if metadata.file_type().is_symlink() {
                return Err(ErrorCode::ArtifactInvalidPath);
            }
            if metadata.is_dir() {
                pending.push((path, depth + 1));
                continue;
            }
            if !metadata.is_file() || metadata.len() == 0 {
                return Err(ErrorCode::ArtifactInvalidPath);
            }
            count = count.checked_add(1).ok_or(ErrorCode::ArtifactTooLarge)?;
            if count > max_files {
                return Err(ErrorCode::ArtifactTooLarge);
            }
            match path.extension().and_then(|value| value.to_str()) {
                Some("mp4") => result.videos.push(path),
                Some("srt") => result.subtitles.push(path),
                Some("xml") => result.danmaku.push(path),
                _ => return Err(ErrorCode::ArtifactInvalidPath),
            }
        }
    }
    Ok(result)
}

fn one(values: &[PathBuf]) -> Result<&PathBuf, ErrorCode> {
    (values.len() == 1)
        .then_some(&values[0])
        .ok_or(ErrorCode::ExecutorFailed)
}

fn optional_one(values: &[PathBuf]) -> Result<Option<&PathBuf>, ErrorCode> {
    (values.len() <= 1)
        .then_some(values.first())
        .ok_or(ErrorCode::ExecutorFailed)
}

fn preferred_subtitle(values: &[PathBuf]) -> Option<&PathBuf> {
    values.iter().min_by_key(|path| {
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        ["zh-Hans", "zh-Hant", ".zh.", ".en."]
            .iter()
            .position(|needle| name.contains(needle))
            .unwrap_or(usize::MAX)
    })
}

async fn copy_regular(source: &Path, target: &Path) -> Result<(), ErrorCode> {
    let metadata = fs::symlink_metadata(source)
        .await
        .map_err(|_| ErrorCode::ExecutorFailed)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() == 0 {
        return Err(ErrorCode::ArtifactInvalidPath);
    }
    fs::copy(source, target)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    Ok(())
}
