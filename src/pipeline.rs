//! Pipeline definitions and the step contract shared by the executor and every step.
//! The authority is contract 1 in docs/contracts.md; keep the two in sync.

use std::{
    collections::BTreeMap,
    fmt, fs, io,
    path::{Path, PathBuf},
    str::FromStr,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

pub const JOB_FILE: &str = "job.json";
pub const MANIFEST_FILE: &str = "manifest.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "lowercase")]
#[sqlx(rename_all = "lowercase")]
pub enum Pipeline {
    Video,
    Arxiv,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "lowercase")]
#[sqlx(rename_all = "lowercase")]
pub enum Platform {
    Bilibili,
    Youtube,
    Arxiv,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "lowercase")]
#[sqlx(rename_all = "lowercase")]
pub enum TranslateMode {
    Auto,
    Off,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "lowercase")]
pub enum AiTool {
    Claude,
    Codex,
}

/// Feed source kinds a collection can bind. Parsing normalizes the URL per kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum FeedKind {
    BiliUp,
    BiliSeason,
    BiliSeries,
    BiliFav,
    YtChannel,
    YtPlaylist,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "kebab-case")]
#[sqlx(rename_all = "kebab-case")]
pub enum StepName {
    VideoFetch,
    VideoTranscribe,
    VideoFrames,
    ArxivFetch,
    ArxivConvert,
    Note,
    Translate,
    Publish,
}

/// Global limit a step waits on. All steps share Semaphore(2); whisper and ai also hold
/// their own Semaphore(1). Local steps skip the global limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Resource {
    Net,
    Whisper,
    Cpu,
    Ai,
    Local,
}

/// How the executor starts a step.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepProgram {
    /// `<current_exe> step <name> <job_dir> <out_dir>`
    BuiltIn,
    /// `python3 -I $FLORI_STEPS_DIR/<script> <subcommand> <job_dir> <out_dir>`
    Python {
        script: &'static str,
        subcommand: &'static str,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StepSpec {
    pub name: StepName,
    pub program: StepProgram,
    pub resource: Resource,
    pub timeout: Duration,
    pub attempts: u32,
}

impl StepName {
    pub const ALL: [StepName; 8] = [
        StepName::VideoFetch,
        StepName::VideoTranscribe,
        StepName::VideoFrames,
        StepName::ArxivFetch,
        StepName::ArxivConvert,
        StepName::Note,
        StepName::Translate,
        StepName::Publish,
    ];

    pub fn as_str(self) -> &'static str {
        match self {
            StepName::VideoFetch => "video-fetch",
            StepName::VideoTranscribe => "video-transcribe",
            StepName::VideoFrames => "video-frames",
            StepName::ArxivFetch => "arxiv-fetch",
            StepName::ArxivConvert => "arxiv-convert",
            StepName::Note => "note",
            StepName::Translate => "translate",
            StepName::Publish => "publish",
        }
    }

    pub fn spec(self) -> StepSpec {
        const MIN: u64 = 60;
        let (program, resource, timeout_s, attempts) = match self {
            StepName::VideoFetch => (StepProgram::BuiltIn, Resource::Net, 30 * MIN, 3),
            StepName::VideoTranscribe => (StepProgram::BuiltIn, Resource::Whisper, 120 * MIN, 2),
            StepName::VideoFrames => (StepProgram::BuiltIn, Resource::Cpu, 60 * MIN, 2),
            StepName::ArxivFetch => (
                StepProgram::Python {
                    script: "arxiv.py",
                    subcommand: "fetch",
                },
                Resource::Net,
                15 * MIN,
                3,
            ),
            StepName::ArxivConvert => (
                StepProgram::Python {
                    script: "arxiv.py",
                    subcommand: "convert",
                },
                Resource::Cpu,
                10 * MIN,
                2,
            ),
            StepName::Note => (StepProgram::BuiltIn, Resource::Ai, 35 * MIN, 1),
            StepName::Translate => (StepProgram::BuiltIn, Resource::Ai, 180 * MIN, 1),
            StepName::Publish => (StepProgram::BuiltIn, Resource::Local, 5 * MIN, 2),
        };
        StepSpec {
            name: self,
            program,
            resource,
            timeout: Duration::from_secs(timeout_s),
            attempts,
        }
    }
}

impl fmt::Display for StepName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

impl FromStr for StepName {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        StepName::ALL
            .into_iter()
            .find(|name| name.as_str() == s)
            .ok_or_else(|| format!("unknown step {s:?}"))
    }
}

/// Steps of one job in execution order. `off` drops translate; `auto` always schedules it
/// and the step itself records `meta.skipped_reason` when the source is Chinese.
pub fn plan(pipeline: Pipeline, translate: TranslateMode) -> Vec<StepSpec> {
    let names: &[StepName] = match pipeline {
        Pipeline::Video => &[
            StepName::VideoFetch,
            StepName::VideoTranscribe,
            StepName::VideoFrames,
            StepName::Note,
            StepName::Translate,
            StepName::Publish,
        ],
        Pipeline::Arxiv => &[
            StepName::ArxivFetch,
            StepName::ArxivConvert,
            StepName::Note,
            StepName::Translate,
            StepName::Publish,
        ],
    };
    names
        .iter()
        .filter(|name| translate == TranslateMode::Auto || **name != StepName::Translate)
        .map(|name| name.spec())
        .collect()
}

/// Exit codes a step may use besides 0. Any other code or a signal means retryable.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepExit {
    Retry = 10,
    Permanent = 20,
    /// Credentials expired. Handled like Permanent; the UI marks it.
    Auth = 30,
}

#[derive(Debug)]
pub struct StepFailure {
    pub exit: StepExit,
    pub message: String,
}

pub type StepResult = Result<(), StepFailure>;

impl StepFailure {
    pub fn retry(message: impl Into<String>) -> Self {
        StepFailure {
            exit: StepExit::Retry,
            message: message.into(),
        }
    }

    pub fn permanent(message: impl Into<String>) -> Self {
        StepFailure {
            exit: StepExit::Permanent,
            message: message.into(),
        }
    }

    pub fn auth(message: impl Into<String>) -> Self {
        StepFailure {
            exit: StepExit::Auth,
            message: message.into(),
        }
    }
}

/// Local IO failures inside a step are bugs or a broken disk, not flaky upstreams.
impl From<io::Error> for StepFailure {
    fn from(err: io::Error) -> Self {
        StepFailure::permanent(format!("io error: {err}"))
    }
}

/// `job.json`, written once by the server when the job is created. Never holds secrets.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JobSpec {
    pub job_id: i64,
    pub pipeline: Pipeline,
    pub source: Source,
    pub domain: String,
    pub collection: Option<String>,
    pub translate: TranslateMode,
    pub ai: AiSpec,
    /// RFC 3339 with +08:00; also fixes the vault folder date.
    pub created: String,
    pub public_url: String,
    pub vault_name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub platform: Platform,
    /// BV id, YouTube video id, or arXiv id without version.
    pub id: String,
    pub url: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AiSpec {
    pub tool: AiTool,
    pub model_note: String,
    pub model_translate: String,
    pub cli_version: String,
}

/// `manifest.json` in every finished step directory.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub title: Option<String>,
    /// Logical name -> path relative to the step directory.
    pub files: BTreeMap<String, String>,
    /// Basenames under the step's `assets/`.
    pub assets: Vec<String>,
    pub meta: BTreeMap<String, MetaValue>,
}

/// A manifest meta value: a JSON scalar or an array of strings.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum MetaValue {
    Bool(bool),
    Int(i64),
    Float(f64),
    Text(String),
    List(Vec<String>),
}

/// Directory name of a step inside `steps/`, e.g. `03-video-frames`.
pub fn step_dir_name(seq: u32, name: StepName) -> String {
    format!("{seq:02}-{name}")
}

pub fn read_job(job_dir: &Path) -> io::Result<JobSpec> {
    read_json(&job_dir.join(JOB_FILE))
}

pub fn read_manifest(step_dir: &Path) -> io::Result<Manifest> {
    read_json(&step_dir.join(MANIFEST_FILE))
}

pub fn write_manifest(out_dir: &Path, manifest: &Manifest) -> io::Result<()> {
    let mut text = serde_json::to_string_pretty(manifest).map_err(io::Error::other)?;
    text.push('\n');
    fs::write(out_dir.join(MANIFEST_FILE), text)
}

/// Finished output directory of an earlier step (`steps/<nn>-<name>/`), if any.
/// In-progress `.tmp` directories never match.
pub fn find_step(job_dir: &Path, name: StepName) -> io::Result<Option<PathBuf>> {
    let steps = job_dir.join("steps");
    let entries = match fs::read_dir(&steps) {
        Ok(entries) => entries,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    for entry in entries {
        let entry = entry?;
        let file_name = entry.file_name();
        let Some(dir) = file_name.to_str() else {
            continue;
        };
        let Some((seq, rest)) = dir.split_once('-') else {
            continue;
        };
        if seq.len() == 2 && seq.bytes().all(|b| b.is_ascii_digit()) && rest == name.as_str() {
            return Ok(Some(entry.path()));
        }
    }
    Ok(None)
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> io::Result<T> {
    let text = fs::read_to_string(path)?;
    serde_json::from_str(&text).map_err(|err| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{}: {err}", path.display()),
        )
    })
}

#[cfg(test)]
mod tests;
