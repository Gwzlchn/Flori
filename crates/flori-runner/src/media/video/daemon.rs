use std::{path::Path, path::PathBuf, time::Duration};

use flori_core::{
    ArtifactKind, ArtifactManifestEntry, ErrorCode, ResolvedArtifact, ResolvedTaskInputs,
    TaskClaim, TranscriptManifest, VideoKeyframe,
};
use tokio::fs;

use crate::{RunnerClient, task_upload};

use super::{
    pdf,
    video::{
        extract_keyframes, mechanical_note_with_frames, normalize_srt, probe_video,
        transcribe::{FasterWhisperConfig, transcribe_video},
    },
    video_claim as claim,
};

pub struct VideoDaemonConfig {
    pub ffmpeg: PathBuf,
    pub ffprobe: PathBuf,
    pub whisper: FasterWhisperConfig,
    pub tool_timeout: Duration,
    pub max_tool_output_bytes: usize,
    pub requested_frames: usize,
    pub max_frame_bytes: usize,
}

pub(crate) async fn run_task(
    client: &RunnerClient,
    config: &VideoDaemonConfig,
    claim: &TaskClaim,
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    claim::validate(claim)?;
    match &claim.resolved_inputs {
        ResolvedTaskInputs::VideoTranscribe { video, subtitle } => {
            transcribe(client, config, claim, video, subtitle.as_ref(), workspace).await
        }
        ResolvedTaskInputs::VideoFrames { video, transcript } => {
            frames(client, config, claim, video, transcript, workspace).await
        }
        ResolvedTaskInputs::VideoMechanicalNote { transcript, frames } => {
            mechanical(client, claim, transcript, frames, workspace).await
        }
        _ => Err(ErrorCode::CorruptState),
    }
}

async fn transcribe(
    client: &RunnerClient,
    config: &VideoDaemonConfig,
    claim: &TaskClaim,
    video: &ResolvedArtifact,
    subtitle: Option<&ResolvedArtifact>,
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let input = workspace.join("source.mp4");
    client
        .download_artifact(video, &input)
        .await
        .map_err(|error| error.code())?;
    let probe = probe_video(
        &config.ffprobe,
        &input,
        config.tool_timeout,
        config.max_tool_output_bytes,
    )
    .await
    .map_err(|error| error.code())?;
    let transcript = if let Some(subtitle) = subtitle {
        let path = workspace.join("subtitle.srt");
        client
            .download_artifact(subtitle, &path)
            .await
            .map_err(|error| error.code())?;
        let text = fs::read_to_string(path)
            .await
            .map_err(|_| ErrorCode::ExecutorFailed)?;
        normalize_srt(video.artifact_id, "und", probe.duration_ms, &text)
            .map_err(|error| error.code())?
    } else {
        transcribe_video(
            &config.whisper,
            &input,
            workspace,
            video.artifact_id,
            probe.duration_ms,
        )
        .await
        .map_err(|error| error.code())?
    };
    let path = workspace.join("transcript.json");
    write_json(&path, &transcript).await?;
    upload_exact(
        client,
        claim,
        ArtifactKind::Transcript,
        "application/json",
        &path,
    )
    .await
}

async fn frames(
    client: &RunnerClient,
    config: &VideoDaemonConfig,
    claim: &TaskClaim,
    video: &ResolvedArtifact,
    transcript: &ResolvedArtifact,
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let input = workspace.join("source.mp4");
    let transcript_path = workspace.join("transcript.json");
    client
        .download_artifact(video, &input)
        .await
        .map_err(|error| error.code())?;
    client
        .download_artifact(transcript, &transcript_path)
        .await
        .map_err(|error| error.code())?;
    let transcript: TranscriptManifest = read_json(&transcript_path).await?;
    if transcript.source_artifact_id != video.artifact_id {
        return Err(ErrorCode::CorruptState);
    }
    let probe = probe_video(
        &config.ffprobe,
        &input,
        config.tool_timeout,
        config.max_tool_output_bytes,
    )
    .await
    .map_err(|error| error.code())?;
    if probe.duration_ms != transcript.duration_ms {
        return Err(ErrorCode::DigestMismatch);
    }
    let outputs = extract_keyframes(
        &config.ffmpeg,
        &input,
        probe.duration_ms,
        probe.frame_rate_num,
        probe.frame_rate_den,
        config.requested_frames,
        config.tool_timeout,
        config.max_frame_bytes,
    )
    .await
    .map_err(|error| error.code())?;
    if outputs.is_empty() {
        return Err(ErrorCode::ExecutorFailed);
    }
    let declaration = pdf::claim::exact(claim, ArtifactKind::Keyframe)?;
    let output_dir = workspace.join("frames");
    fs::create_dir(&output_dir)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let mut entries = Vec::with_capacity(outputs.len());
    for output in outputs {
        let path = output_dir.join(pdf::claim::basename(&output.logical_name)?);
        fs::write(&path, output.bytes)
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        entries.push(
            task_upload::file(
                client,
                claim,
                declaration,
                output.logical_name,
                "image/jpeg",
                &path,
            )
            .await?,
        );
    }
    Ok(entries)
}

async fn mechanical(
    client: &RunnerClient,
    claim: &TaskClaim,
    transcript: &ResolvedArtifact,
    frames: &[ResolvedArtifact],
    workspace: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let path = workspace.join("transcript.json");
    client
        .download_artifact(transcript, &path)
        .await
        .map_err(|error| error.code())?;
    let transcript: TranscriptManifest = read_json(&path).await?;
    let keyframes = frames
        .iter()
        .map(|frame| {
            VideoKeyframe::from_artifact_name(frame.artifact_id, &frame.name)
                .map_err(|_| ErrorCode::CorruptState)
        })
        .collect::<Result<Vec<_>, _>>()?;
    let note =
        mechanical_note_with_frames(&transcript, &keyframes).map_err(|error| error.code())?;
    let output = workspace.join("mechanical-note.md");
    fs::write(&output, note)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    upload_exact(
        client,
        claim,
        ArtifactKind::MechanicalNote,
        "text/markdown",
        &output,
    )
    .await
}

async fn upload_exact(
    client: &RunnerClient,
    claim: &TaskClaim,
    kind: ArtifactKind,
    media_type: &str,
    path: &Path,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let declaration = pdf::claim::exact(claim, kind)?;
    Ok(vec![
        task_upload::file(
            client,
            claim,
            declaration,
            declaration.name.clone(),
            media_type,
            path,
        )
        .await?,
    ])
}

async fn write_json(path: &Path, value: &impl serde::Serialize) -> Result<(), ErrorCode> {
    let bytes = serde_json::to_vec(value).map_err(|_| ErrorCode::Internal)?;
    fs::write(path, bytes)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)
}

async fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, ErrorCode> {
    let bytes = fs::read(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    serde_json::from_slice(&bytes).map_err(|_| ErrorCode::CorruptState)
}
