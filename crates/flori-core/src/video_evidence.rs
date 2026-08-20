use std::collections::BTreeSet;

use crate::evidence::{normalize, validate_note_outputs};
use crate::{
    ErrorCode, EvidenceLocator, EvidenceManifest, TermsManifest, TranscriptManifest, VideoKeyframe,
};

pub fn validate_video_evidence(
    transcript: &TranscriptManifest,
    keyframes: &[VideoKeyframe],
    frame_duration_ms: u64,
    terms: &TermsManifest,
    smart_note: &str,
    summary: &str,
) -> Result<EvidenceManifest, ErrorCode> {
    transcript
        .validate()
        .map_err(|_| ErrorCode::EvidenceInvalid)?;
    if frame_duration_ms == 0 || !valid_keyframes(keyframes, transcript.duration_ms) {
        return Err(ErrorCode::EvidenceInvalid);
    }
    let manifest = validate_note_outputs(terms, smart_note, summary)?;
    for item in &manifest.items {
        let EvidenceLocator::Video {
            start_ms,
            end_ms,
            keyframe,
        } = &item.locator
        else {
            return Err(ErrorCode::EvidenceInvalid);
        };
        if item.source_artifact_id != transcript.source_artifact_id
            || *end_ms > transcript.duration_ms
            || !quote_matches(transcript, *start_ms, *end_ms, &item.quote)
            || keyframe.as_ref().is_some_and(|selected| {
                !keyframes.contains(selected)
                    || distance_to_range(selected.timestamp_ms, *start_ms, *end_ms)
                        > frame_duration_ms
            })
        {
            return Err(ErrorCode::EvidenceInvalid);
        }
    }
    Ok(manifest)
}

fn valid_keyframes(keyframes: &[VideoKeyframe], duration_ms: u64) -> bool {
    let mut ids = BTreeSet::new();
    let mut timestamps = BTreeSet::new();
    keyframes.iter().all(|frame| {
        frame.timestamp_ms <= duration_ms
            && ids.insert(frame.artifact_id)
            && timestamps.insert(frame.timestamp_ms)
    })
}

fn quote_matches(transcript: &TranscriptManifest, start_ms: u64, end_ms: u64, quote: &str) -> bool {
    let source = transcript
        .cues
        .iter()
        .filter(|cue| cue.start_ms < end_ms && start_ms < cue.end_ms)
        .map(|cue| cue.text.as_str())
        .collect::<Vec<_>>()
        .join(" ");
    let quote = normalize(quote);
    !quote.is_empty() && normalize(&source).contains(&quote)
}

fn distance_to_range(timestamp_ms: u64, start_ms: u64, end_ms: u64) -> u64 {
    if timestamp_ms < start_ms {
        start_ms - timestamp_ms
    } else {
        timestamp_ms.saturating_sub(end_ms)
    }
}
