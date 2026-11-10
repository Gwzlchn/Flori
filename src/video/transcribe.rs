//! video-transcribe: a subtitle normalized into transcript.json, or whisper on the audio.

use std::{collections::BTreeMap, ffi::OsString, fs, path::Path};

use serde::{Deserialize, de::DeserializeOwned};
use tokio::process::Command;

use super::{Segment, Transcript, env_value, fetch::AI_SUBTITLE, push_text, run_checked};
use crate::pipeline::{
    Manifest, MetaValue, StepFailure, StepName, StepResult, prior_step, read_json, step_file,
    write_json, write_manifest,
};

pub(super) const TRANSCRIPT_JSON: &str = "transcript.json";

/// `whisper.py transcribe` output: transcript.json plus the language detection confidence.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct WhisperOutput {
    pub(super) language: String,
    pub(super) language_probability: f64,
    pub(super) segments: Vec<Segment>,
}

/// `whisper.py detect` output: the spoken language without decoding any text.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Detected {
    language: String,
    language_probability: f64,
}

/// What video-transcribe runs. Tests point the tools at fakes.
pub(super) struct TranscribeEnv {
    pub(super) ffmpeg: OsString,
    pub(super) python: OsString,
    pub(super) steps_dir: Option<String>,
}

impl TranscribeEnv {
    fn inherited() -> TranscribeEnv {
        TranscribeEnv {
            ffmpeg: "ffmpeg".into(),
            python: "python3".into(),
            steps_dir: env_value("FLORI_STEPS_DIR"),
        }
    }
}

/// The transcript and where it came from, for meta.origin and meta.subtitle_kind.
struct Chosen {
    transcript: Transcript,
    origin: &'static str,
    subtitle_kind: Option<&'static str>,
    probability: f64,
}

pub async fn transcribe(job_dir: &Path, out_dir: &Path) -> StepResult {
    transcribe_with(job_dir, out_dir, &TranscribeEnv::inherited()).await
}

pub(super) async fn transcribe_with(
    job_dir: &Path,
    out_dir: &Path,
    env: &TranscribeEnv,
) -> StepResult {
    let (fetch_dir, fetched) = prior_step(job_dir, StepName::VideoFetch)?;
    let chosen = match fetched.files.get("subtitle") {
        Some(subtitle) => {
            let language = match fetched.meta.get("subtitle_language") {
                Some(MetaValue::Text(lang)) => Some(lang.clone()),
                _ => None,
            };
            Chosen {
                transcript: Transcript {
                    language,
                    segments: read_subtitle(&fetch_dir.join(subtitle))?,
                },
                origin: "subtitle",
                subtitle_kind: Some("manual"),
                probability: 1.0,
            }
        }
        None => {
            let ai_tracks: BTreeMap<&str, &String> = fetched
                .files
                .iter()
                .filter_map(|(name, file)| Some((name.strip_prefix(AI_SUBTITLE)?, file)))
                .collect();
            let video = step_file(job_dir, StepName::VideoFetch, "video")?;
            from_audio(&video, &fetch_dir, &ai_tracks, out_dir, env).await?
        }
    };
    write_json(&out_dir.join(TRANSCRIPT_JSON), &chosen.transcript)?;
    let language = chosen
        .transcript
        .language
        .unwrap_or_else(|| "unknown".into());
    let mut meta = BTreeMap::from([
        ("origin".into(), MetaValue::Text(chosen.origin.into())),
        ("language".into(), MetaValue::Text(language)),
        (
            "language_probability".into(),
            MetaValue::Float(chosen.probability),
        ),
    ]);
    if let Some(kind) = chosen.subtitle_kind {
        meta.insert("subtitle_kind".into(), MetaValue::Text(kind.into()));
    }
    let manifest = Manifest {
        title: None,
        files: BTreeMap::from([("transcript_json".into(), TRANSCRIPT_JSON.into())]),
        assets: Vec::new(),
        meta,
    };
    write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// 16 kHz mono WAV through ffmpeg, then the Bilibili AI track in the language whisper detects in
/// it, or else whisper's own transcription. An AI track in any other language is a machine
/// translation of the speech. The scratch files live in out_dir so a failed attempt is cleaned
/// up with it.
async fn from_audio(
    video: &Path,
    fetch_dir: &Path,
    ai_tracks: &BTreeMap<&str, &String>,
    out_dir: &Path,
    env: &TranscribeEnv,
) -> Result<Chosen, StepFailure> {
    let audio = out_dir.join("audio.wav");
    let mut ffmpeg = Command::new(&env.ffmpeg);
    ffmpeg
        .args(["-hide_banner", "-nostdin", "-loglevel", "error", "-i"])
        .arg(video)
        .args(["-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000"])
        .args(["-c:a", "pcm_s16le", "-y"])
        .arg(&audio);
    run_checked(ffmpeg, "ffmpeg").await?;
    if !ai_tracks.is_empty() {
        let detected: Detected = run_whisper(env, "detect", &audio, out_dir).await?;
        let (language, p) = (detected.language, detected.language_probability);
        if let Some(file) = ai_tracks.get(language.as_str()) {
            eprintln!("video-transcribe: audio is {language} p={p:.2}, using the AI subtitle");
            let segments = read_subtitle(&fetch_dir.join(file))?;
            fs::remove_file(&audio)?;
            return Ok(Chosen {
                transcript: Transcript {
                    language: Some(language),
                    segments,
                },
                origin: "subtitle",
                subtitle_kind: Some("ai"),
                probability: p,
            });
        }
        eprintln!("video-transcribe: audio is {language} p={p:.2}, no AI subtitle in it");
    }
    let whisper: WhisperOutput = run_whisper(env, "transcribe", &audio, out_dir).await?;
    fs::remove_file(&audio)?;
    Ok(Chosen {
        transcript: Transcript {
            language: Some(whisper.language),
            segments: whisper.segments,
        },
        origin: "whisper",
        subtitle_kind: None,
        probability: whisper.language_probability,
    })
}

/// `python3 -I $FLORI_STEPS_DIR/whisper.py <mode> <wav> <json>` and its parsed output.
async fn run_whisper<T: DeserializeOwned>(
    env: &TranscribeEnv,
    mode: &str,
    audio: &Path,
    out_dir: &Path,
) -> Result<T, StepFailure> {
    let steps = env
        .steps_dir
        .as_deref()
        .ok_or_else(|| StepFailure::permanent("FLORI_STEPS_DIR is not set"))?;
    let raw = out_dir.join("whisper.json");
    let mut python = Command::new(&env.python);
    python
        .arg("-I")
        .arg(Path::new(steps).join("whisper.py"))
        .arg(mode)
        .arg(audio)
        .arg(&raw);
    run_checked(python, "whisper.py").await?;
    let output = read_json(&raw)?;
    fs::remove_file(&raw)?;
    Ok(output)
}

fn read_subtitle(path: &Path) -> Result<Vec<Segment>, StepFailure> {
    let segments = parse_srt(&fs::read_to_string(path)?);
    if segments.is_empty() {
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        return Err(StepFailure::permanent(format!("{name} has no usable cues")));
    }
    Ok(segments)
}

/// Lenient SRT reader: index lines and unparseable blocks are skipped, markup is stripped and
/// cues without text are dropped.
pub(crate) fn parse_srt(text: &str) -> Vec<Segment> {
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
