use std::{ffi::OsString, path::PathBuf, time::Duration};

use flori_core::{ArtifactId, TranscriptCue, TranscriptManifest, TranscriptSchema};
use serde::Deserialize;
use tokio::fs;

use super::{VideoMediaError, process::run_tool};

const TRANSCRIBER: &[u8] = include_bytes!("transcriber.py");
pub(crate) const FASTER_WHISPER_VERSION: &str = "1.2.1";
pub(crate) const WHISPER_MODEL_NAME: &str = "base";

#[derive(Clone, Debug)]
pub(crate) struct FasterWhisperConfig {
    pub python: PathBuf,
    pub model_dir: PathBuf,
    pub model_name: String,
    pub timeout: Duration,
    pub max_output_bytes: usize,
}

pub(crate) async fn transcribe_video(
    config: &FasterWhisperConfig,
    input: &std::path::Path,
    workspace: &std::path::Path,
    source_artifact_id: ArtifactId,
    duration_ms: u64,
) -> Result<TranscriptManifest, VideoMediaError> {
    validate_config(config).await?;
    if !input.is_absolute() || !workspace.is_absolute() || duration_ms == 0 {
        return Err(VideoMediaError::InvalidTranscriber);
    }
    let script = workspace.join("transcriber.py");
    fs::write(&script, TRANSCRIBER)
        .await
        .map_err(|_| VideoMediaError::ToolFailed)?;
    let arguments = [
        OsString::from("-I"),
        script.as_os_str().to_owned(),
        config.model_dir.as_os_str().to_owned(),
        OsString::from(&config.model_name),
        input.as_os_str().to_owned(),
    ];
    let output = run_tool(
        &config.python,
        &arguments,
        config.timeout,
        config.max_output_bytes,
    )
    .await;
    let _ = fs::remove_file(&script).await;
    let raw: TranscriberOutput =
        serde_json::from_slice(&output?).map_err(|_| VideoMediaError::InvalidTranscriber)?;
    let transcript = TranscriptManifest {
        schema: TranscriptSchema::V1,
        source_artifact_id,
        language: raw.language,
        duration_ms,
        cues: raw
            .segments
            .into_iter()
            .map(|segment| TranscriptCue {
                start_ms: segment.start_ms,
                end_ms: segment.end_ms,
                text: segment.text,
            })
            .collect(),
    };
    transcript
        .validate()
        .map_err(|_| VideoMediaError::InvalidTranscriber)?;
    Ok(transcript)
}

async fn validate_config(config: &FasterWhisperConfig) -> Result<(), VideoMediaError> {
    if !config.python.is_absolute()
        || !config.model_dir.is_absolute()
        || config.model_name != WHISPER_MODEL_NAME
        || config.model_dir.file_name().and_then(|name| name.to_str()) != Some(WHISPER_MODEL_NAME)
        || config.timeout.is_zero()
        || config.max_output_bytes == 0
    {
        return Err(VideoMediaError::InvalidTranscriber);
    }
    let root = fs::symlink_metadata(&config.model_dir)
        .await
        .map_err(|_| VideoMediaError::InvalidTranscriber)?;
    if root.file_type().is_symlink() || !root.is_dir() {
        return Err(VideoMediaError::InvalidTranscriber);
    }
    for name in ["config.json", "model.bin", "tokenizer.json"] {
        require_file(&config.model_dir.join(name)).await?;
    }
    if require_file(&config.model_dir.join("vocabulary.json"))
        .await
        .is_err()
        && require_file(&config.model_dir.join("vocabulary.txt"))
            .await
            .is_err()
    {
        return Err(VideoMediaError::InvalidTranscriber);
    }
    Ok(())
}

async fn require_file(path: &std::path::Path) -> Result<(), VideoMediaError> {
    let metadata = fs::symlink_metadata(path)
        .await
        .map_err(|_| VideoMediaError::InvalidTranscriber)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() == 0 {
        return Err(VideoMediaError::InvalidTranscriber);
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TranscriberOutput {
    language: String,
    segments: Vec<TranscriberSegment>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TranscriberSegment {
    start_ms: u64,
    end_ms: u64,
    text: String,
}
