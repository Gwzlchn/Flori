use std::path::{Path, PathBuf};

use flori_core::{
    DocumentStructure, ErrorCode, Executor, ResolvedArtifact, ResolvedProfile, ResolvedPrompt,
    ResolvedTaskInputs, Sha256Digest, TranscriptManifest, VideoKeyframe,
};
use sha2::{Digest, Sha256};
use tokio::fs;

use crate::RunnerClient;

pub(super) struct PreparedInput {
    pub prompt: String,
    pub workspace: PathBuf,
    pub evidence: Option<EvidenceSource>,
}

pub(super) enum EvidenceSource {
    Document(DocumentStructure),
    Video {
        transcript: TranscriptManifest,
        keyframes: Vec<VideoKeyframe>,
    },
}

pub(super) async fn prepare(
    client: &RunnerClient,
    executor: Executor,
    prompt_snapshot_sha256: &Sha256Digest,
    inputs: &ResolvedTaskInputs,
    workspace: PathBuf,
) -> Result<PreparedInput, ErrorCode> {
    fs::create_dir(&workspace)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let input_dir = workspace.join("inputs");
    fs::create_dir(&input_dir)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;

    let (prompt, evidence) = match (executor, inputs) {
        (
            Executor::AiDocumentTranslate,
            ResolvedTaskInputs::AiDocumentTranslate {
                document,
                prompt,
                profile,
            },
        ) => {
            let document_text =
                download_text(client, document, &input_dir.join("document.json")).await?;
            (
                compose(
                    executor,
                    prompt_snapshot_sha256,
                    prompt,
                    profile.as_ref(),
                    document,
                    &document_text,
                )?,
                None,
            )
        }
        (
            Executor::AiDocumentNote,
            ResolvedTaskInputs::AiDocumentNote {
                document,
                prompt,
                profile,
            },
        ) => {
            let document_text =
                download_text(client, document, &input_dir.join("document.json")).await?;
            let structure = serde_json::from_str::<DocumentStructure>(&document_text)
                .map_err(|_| ErrorCode::EvidenceInvalid)?;
            structure
                .validate()
                .map_err(|_| ErrorCode::EvidenceInvalid)?;
            (
                compose(
                    executor,
                    prompt_snapshot_sha256,
                    prompt,
                    profile.as_ref(),
                    document,
                    &document_text,
                )?,
                Some(EvidenceSource::Document(structure)),
            )
        }
        (
            Executor::AiVideoNote,
            ResolvedTaskInputs::AiVideoNote {
                transcript,
                mechanical_note,
                frames,
                prompt,
                profile,
            },
        ) => {
            let transcript_text =
                download_text(client, transcript, &input_dir.join("transcript.json")).await?;
            let transcript_value = serde_json::from_str::<TranscriptManifest>(&transcript_text)
                .map_err(|_| ErrorCode::EvidenceInvalid)?;
            transcript_value
                .validate()
                .map_err(|_| ErrorCode::EvidenceInvalid)?;
            let mechanical_text = download_text(
                client,
                mechanical_note,
                &input_dir.join("mechanical-note.md"),
            )
            .await?;
            let mut keyframes = Vec::with_capacity(frames.len());
            let mut frame_text = String::new();
            for frame in frames {
                let keyframe = VideoKeyframe::from_artifact_name(frame.artifact_id, &frame.name)
                    .map_err(|_| ErrorCode::EvidenceInvalid)?;
                let path = input_dir.join(format!("keyframe-{:013}.jpg", keyframe.timestamp_ms));
                client
                    .download_artifact(frame, &path)
                    .await
                    .map_err(|error| error.code())?;
                frame_text.push_str(&format!(
                    "{} {} {} {}\n",
                    frame.name,
                    frame.artifact_id,
                    keyframe.timestamp_ms,
                    path.display()
                ));
                keyframes.push(keyframe);
            }
            (
                compose_video(
                    executor,
                    prompt_snapshot_sha256,
                    prompt,
                    profile.as_ref(),
                    transcript,
                    &transcript_text,
                    mechanical_note,
                    &mechanical_text,
                    &frame_text,
                )?,
                Some(EvidenceSource::Video {
                    transcript: transcript_value,
                    keyframes,
                }),
            )
        }
        _ => return Err(ErrorCode::CorruptState),
    };
    Ok(PreparedInput {
        prompt,
        workspace,
        evidence,
    })
}

#[allow(clippy::too_many_arguments)]
fn compose_video(
    executor: Executor,
    prompt_snapshot_sha256: &Sha256Digest,
    prompt: &ResolvedPrompt,
    profile: Option<&ResolvedProfile>,
    transcript: &ResolvedArtifact,
    transcript_text: &str,
    mechanical: &ResolvedArtifact,
    mechanical_text: &str,
    frames: &str,
) -> Result<String, ErrorCode> {
    verify(prompt.content.as_bytes(), &prompt.sha256)?;
    if let Some(profile) = profile {
        verify(profile.content.as_bytes(), &profile.sha256)?;
    }
    let executor = serde_json::to_string(&executor).map_err(|_| ErrorCode::Internal)?;
    let mut composed = String::new();
    section(&mut composed, "EXECUTOR", executor.trim_matches('"'));
    section(
        &mut composed,
        "PROMPT SNAPSHOT SHA256",
        prompt_snapshot_sha256.as_str(),
    );
    section(&mut composed, "PROMPT", &prompt.content);
    if let Some(profile) = profile {
        section(&mut composed, "DOMAIN PROFILE", &profile.content);
    }
    section(
        &mut composed,
        "TRANSCRIPT ARTIFACT ID",
        &transcript.artifact_id.to_string(),
    );
    section(
        &mut composed,
        "TRANSCRIPT SHA256",
        transcript.sha256.as_str(),
    );
    section(&mut composed, "TRANSCRIPT", transcript_text);
    section(
        &mut composed,
        "MECHANICAL NOTE SHA256",
        mechanical.sha256.as_str(),
    );
    section(&mut composed, "MECHANICAL NOTE", mechanical_text);
    section(&mut composed, "KEYFRAMES", frames);
    Ok(composed)
}

async fn download_text(
    client: &RunnerClient,
    artifact: &ResolvedArtifact,
    destination: &Path,
) -> Result<String, ErrorCode> {
    let expected = usize::try_from(artifact.size_bytes).map_err(|_| ErrorCode::ArtifactTooLarge)?;
    client
        .download_artifact(artifact, destination)
        .await
        .map_err(|error| error.code())?;
    let bytes = fs::read(destination)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    if bytes.len() != expected {
        return Err(ErrorCode::DigestMismatch);
    }
    String::from_utf8(bytes).map_err(|_| ErrorCode::CorruptState)
}

fn compose(
    executor: Executor,
    prompt_snapshot_sha256: &Sha256Digest,
    prompt: &ResolvedPrompt,
    profile: Option<&ResolvedProfile>,
    document: &ResolvedArtifact,
    document_text: &str,
) -> Result<String, ErrorCode> {
    verify(prompt.content.as_bytes(), &prompt.sha256)?;
    if let Some(profile) = profile {
        verify(profile.content.as_bytes(), &profile.sha256)?;
    }
    let executor = serde_json::to_string(&executor).map_err(|_| ErrorCode::Internal)?;
    let mut composed = String::new();
    section(&mut composed, "EXECUTOR", executor.trim_matches('"'));
    section(
        &mut composed,
        "PROMPT SNAPSHOT SHA256",
        prompt_snapshot_sha256.as_str(),
    );
    section(&mut composed, "PROMPT", &prompt.content);
    if let Some(profile) = profile {
        section(&mut composed, "DOMAIN PROFILE", &profile.content);
    }
    section(&mut composed, "DOCUMENT NAME", &document.name);
    section(&mut composed, "DOCUMENT MEDIA TYPE", &document.media_type);
    section(&mut composed, "DOCUMENT SHA256", document.sha256.as_str());
    section(&mut composed, "DOCUMENT", document_text);
    Ok(composed)
}

fn section(target: &mut String, name: &str, content: &str) {
    target.push_str(name);
    target.push(' ');
    target.push_str(&content.len().to_string());
    target.push('\n');
    target.push_str(content);
    target.push('\n');
}

fn verify(bytes: &[u8], expected: &flori_core::Sha256Digest) -> Result<(), ErrorCode> {
    let actual = Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    (actual == expected.as_str())
        .then_some(())
        .ok_or(ErrorCode::DigestMismatch)
}
