use flori_core::{
    ArtifactKind, ArtifactWhen, ErrorCode, Executor, ResolvedTaskInputs, SourceKind, TaskClaim,
};

use super::pdf::claim::exact;

pub(crate) fn validate(claim: &TaskClaim) -> Result<(), ErrorCode> {
    if claim.timeout_ms == 0
        || claim.attempt_no == 0
        || claim.task_key.is_empty()
        || claim.model.is_some()
        || claim.effort.is_some()
        || claim.secret_inputs.credential.is_some()
    {
        return Err(ErrorCode::CorruptState);
    }
    match (&claim.executor, &claim.resolved_inputs) {
        (Executor::VideoAcquire, ResolvedTaskInputs::VideoAcquire { source })
            if source.kind == SourceKind::LocalVideo
                && source.input.as_ref().is_some_and(|input| {
                    input.media_type == "video/mp4" && input.name == "original"
                }) =>
        {
            shape(
                claim,
                &[
                    (
                        ArtifactKind::SourceOriginal,
                        "videos",
                        "output/videos/*",
                        true,
                        true,
                    ),
                    (
                        ArtifactKind::Subtitle,
                        "subtitle",
                        "output/subtitle.srt",
                        false,
                        false,
                    ),
                    (
                        ArtifactKind::Danmaku,
                        "danmaku",
                        "output/danmaku.xml",
                        false,
                        false,
                    ),
                    (
                        ArtifactKind::PartsManifest,
                        "parts",
                        "output/parts.json",
                        true,
                        false,
                    ),
                ],
            )
        }
        (Executor::VideoTranscribe, ResolvedTaskInputs::VideoTranscribe { video, subtitle })
            if video.kind == ArtifactKind::SourceOriginal
                && video.media_type == "video/mp4"
                && subtitle
                    .as_ref()
                    .is_none_or(|item| item.kind == ArtifactKind::Subtitle) =>
        {
            shape(
                claim,
                &[(
                    ArtifactKind::Transcript,
                    "transcript",
                    "output/transcript.json",
                    true,
                    false,
                )],
            )
        }
        (Executor::VideoFrames, ResolvedTaskInputs::VideoFrames { video, transcript })
            if video.kind == ArtifactKind::SourceOriginal
                && video.media_type == "video/mp4"
                && transcript.kind == ArtifactKind::Transcript
                && transcript.media_type == "application/json" =>
        {
            shape(
                claim,
                &[(
                    ArtifactKind::Keyframe,
                    "frames",
                    "output/frames/*",
                    true,
                    true,
                )],
            )
        }
        (
            Executor::VideoMechanicalNote,
            ResolvedTaskInputs::VideoMechanicalNote { transcript, frames },
        ) if transcript.kind == ArtifactKind::Transcript
            && transcript.media_type == "application/json"
            && !frames.is_empty()
            && frames.iter().all(|item| {
                item.kind == ArtifactKind::Keyframe && item.media_type == "image/jpeg"
            }) =>
        {
            shape(
                claim,
                &[(
                    ArtifactKind::MechanicalNote,
                    "mechanical_note",
                    "output/mechanical-note.md",
                    true,
                    false,
                )],
            )
        }
        _ => Err(ErrorCode::CorruptState),
    }
}

fn shape(
    claim: &TaskClaim,
    business: &[(ArtifactKind, &str, &str, bool, bool)],
) -> Result<(), ErrorCode> {
    if claim.output_declarations.len() != business.len() + 1 {
        return Err(ErrorCode::CorruptState);
    }
    for &(kind, name, path, required, many) in business {
        let declaration = exact(claim, kind)?;
        if declaration.name != name
            || declaration.path != path
            || declaration.required != required
            || declaration.when != ArtifactWhen::OnSuccess
            || declaration.max_bytes == 0
            || declaration.max_files.is_some() != many
            || declaration.max_files == Some(0)
        {
            return Err(ErrorCode::CorruptState);
        }
    }
    let log = exact(claim, ArtifactKind::TaskLog)?;
    if log.name != "log"
        || log.path != "logs/task.ndjson"
        || !log.required
        || log.when != ArtifactWhen::Always
        || log.max_files.is_some()
        || log.max_bytes == 0
    {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}
