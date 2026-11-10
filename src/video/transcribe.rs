//! video-transcribe: a subtitle normalized into transcript.json, or whisper on the audio.

use std::{collections::BTreeMap, fs, path::Path};

use serde::Deserialize;
use tokio::process::Command;

use super::{Segment, Transcript, env_value, push_text, run_checked};
use crate::pipeline::{
    Manifest, MetaValue, StepFailure, StepName, StepResult, prior_step, read_json, step_file,
    write_json, write_manifest,
};

pub(super) const TRANSCRIPT_JSON: &str = "transcript.json";

/// whisper.py output: transcript.json plus the language detection confidence.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct WhisperOutput {
    pub(super) language: String,
    pub(super) language_probability: f64,
    pub(super) segments: Vec<Segment>,
}

pub async fn transcribe(job_dir: &Path, out_dir: &Path) -> StepResult {
    let (fetch_dir, fetched) = prior_step(job_dir, StepName::VideoFetch)?;
    let (transcript, origin, probability) = match fetched.files.get("subtitle") {
        Some(subtitle) => {
            let segments = parse_srt(&fs::read_to_string(fetch_dir.join(subtitle))?);
            if segments.is_empty() {
                return Err(StepFailure::permanent("subtitle.srt has no usable cues"));
            }
            let language = match fetched.meta.get("subtitle_language") {
                Some(MetaValue::Text(lang)) => Some(lang.clone()),
                _ => None,
            };
            (Transcript { language, segments }, "subtitle", 1.0)
        }
        None => {
            let video = step_file(job_dir, StepName::VideoFetch, "video")?;
            let whisper = run_whisper(&video, out_dir).await?;
            let transcript = Transcript {
                language: Some(whisper.language),
                segments: whisper.segments,
            };
            (transcript, "whisper", whisper.language_probability)
        }
    };
    write_json(&out_dir.join(TRANSCRIPT_JSON), &transcript)?;
    let language = transcript.language.unwrap_or_else(|| "unknown".into());
    let manifest = Manifest {
        title: None,
        files: BTreeMap::from([("transcript_json".into(), TRANSCRIPT_JSON.into())]),
        assets: Vec::new(),
        meta: BTreeMap::from([
            ("origin".into(), MetaValue::Text(origin.into())),
            ("language".into(), MetaValue::Text(language)),
            ("language_probability".into(), MetaValue::Float(probability)),
        ]),
    };
    write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// 16 kHz mono WAV through ffmpeg, then `python3 -I $FLORI_STEPS_DIR/whisper.py <wav> <json>`.
/// The scratch files live in out_dir so a failed attempt is cleaned up with it.
async fn run_whisper(video: &Path, out_dir: &Path) -> Result<WhisperOutput, StepFailure> {
    let audio = out_dir.join("audio.wav");
    let raw = out_dir.join("whisper.json");
    let mut ffmpeg = Command::new("ffmpeg");
    ffmpeg
        .args(["-hide_banner", "-nostdin", "-loglevel", "error", "-i"])
        .arg(video)
        .args(["-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000"])
        .args(["-c:a", "pcm_s16le", "-y"])
        .arg(&audio);
    run_checked(ffmpeg, "ffmpeg").await?;
    let steps = env_value("FLORI_STEPS_DIR")
        .ok_or_else(|| StepFailure::permanent("FLORI_STEPS_DIR is not set"))?;
    let mut python = Command::new("python3");
    python
        .arg("-I")
        .arg(Path::new(&steps).join("whisper.py"))
        .arg(&audio)
        .arg(&raw);
    run_checked(python, "whisper.py").await?;
    let output = read_json(&raw)?;
    fs::remove_file(&audio)?;
    fs::remove_file(&raw)?;
    Ok(output)
}

/// Lenient SRT reader: index lines and unparseable blocks are skipped, markup is stripped and
/// cues without text are dropped.
pub(super) fn parse_srt(text: &str) -> Vec<Segment> {
    let mut segments = Vec::new();
    let mut current: Option<Segment> = None;
    for line in text.trim_start_matches('\u{feff}').lines() {
        let line = line.trim();
        if let Some((start_ms, end_ms)) = parse_timing(line) {
            segments.extend(current.take());
            current = Some(Segment {
                start_ms,
                end_ms: end_ms.max(start_ms),
                text: String::new(),
            });
        } else if line.is_empty() {
            segments.extend(current.take());
        } else if let Some(segment) = &mut current {
            push_text(&mut segment.text, &strip_markup(line));
        }
    }
    segments.extend(current);
    segments.retain(|segment| !segment.text.is_empty());
    segments
}

fn parse_timing(line: &str) -> Option<(u64, u64)> {
    let (start, end) = line.split_once("-->")?;
    Some((parse_srt_time(start)?, parse_srt_time(end)?))
}

/// `HH:MM:SS,mmm`, also `MM:SS.mmm`; trailing position settings are ignored.
pub(super) fn parse_srt_time(value: &str) -> Option<u64> {
    let value = value.split_whitespace().next()?;
    let (clock, fraction) = value.split_once([',', '.'])?;
    let fields: Vec<&str> = clock.split(':').collect();
    if !(2..=3).contains(&fields.len())
        || !(1..=3).contains(&fraction.len())
        || !fraction.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    let mut seconds = 0_u64;
    for field in fields {
        if field.is_empty() || !field.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        seconds = seconds.checked_mul(60)?.checked_add(field.parse().ok()?)?;
    }
    let millis = fraction.parse::<u64>().ok()? * 10_u64.pow(3 - fraction.len() as u32);
    seconds.checked_mul(1000)?.checked_add(millis)
}

/// Drops the subtitle markup yt-dlp and ffmpeg leave in SRT: `<i>`, `<b>`, `<u>`, `<s>`,
/// `<font ...>`, WebVTT `<c.class>` and ASS `{\an8}`. Other angle brackets and braces are
/// spoken text, such as `Vec<String>` or `a < b`, and stay.
fn strip_markup(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut rest = line;
    while let Some(open) = rest.find(['<', '{']) {
        out.push_str(&rest[..open]);
        rest = &rest[open..];
        // `<` and `{` are one byte, so slicing past them stays on a char boundary.
        let skip = match markup_len(rest) {
            Some(len) => len,
            None => {
                out.push_str(&rest[..1]);
                1
            }
        };
        rest = &rest[skip..];
    }
    out.push_str(rest);
    out
}

/// Byte length of the markup `text` starts with, if it is one.
fn markup_len(text: &str) -> Option<usize> {
    if text.starts_with("{\\") {
        return text.find('}').map(|end| end + 1);
    }
    let tag = text.strip_prefix('<')?;
    let tag = tag.strip_prefix('/').unwrap_or(tag);
    let name_len = tag
        .find(|c: char| !c.is_ascii_alphabetic())
        .unwrap_or(tag.len());
    let (name, after) = tag.split_at(name_len);
    let known = ["i", "b", "u", "s", "c", "font"].contains(&name.to_ascii_lowercase().as_str());
    if !known || !after.starts_with(['>', ' ', '.']) {
        return None;
    }
    text.find('>').map(|end| end + 1)
}
