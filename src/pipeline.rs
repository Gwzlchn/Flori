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
use sha2::{Digest, Sha256};
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
/// and the step itself decides: a Chinese paper is skipped with `meta.skipped_reason`, a Chinese
/// video gets punctuation instead of a translation.
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

/// Also what `GET /api/jobs/{id}` reports as the job's AI settings (contract 3).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct AiSpec {
    pub tool: AiTool,
    pub model_note: String,
    pub model_translate: String,
    pub cli_version: String,
}

impl Platform {
    /// The serialized value, as in job.json and the database.
    pub fn as_str(self) -> &'static str {
        match self {
            Platform::Bilibili => "bilibili",
            Platform::Youtube => "youtube",
            Platform::Arxiv => "arxiv",
        }
    }
}

/// `<bili|yt|arxiv>-<source id>`, the tail of the vault folder name and the head of the job key.
/// Old-style arXiv ids carry a `/`, which a file name cannot.
pub fn source_slug(platform: Platform, source_id: &str) -> String {
    let prefix = match platform {
        Platform::Bilibili => "bili",
        Platform::Youtube => "yt",
        Platform::Arxiv => "arxiv",
    };
    format!("{prefix}-{}", source_id.replace('/', "_"))
}

/// The job's string id: `<slug>-<first 6 hex of sha256("<platform>:<source_id>:<created>")>`,
/// e.g. `yt-dQw4w9WgXcQ-3f9a2c`. All three inputs are in job.json and never change, so the key
/// survives a wiped database, where the integer id starts over. `created` is hashed as written,
/// not reparsed: another spelling of the same instant would give another key.
pub fn job_key(platform: Platform, source_id: &str, created: &str) -> String {
    let digest = Sha256::digest(format!("{}:{source_id}:{created}", platform.as_str()));
    let hash: String = digest[..3].iter().map(|b| format!("{b:02x}")).collect();
    format!("{}-{hash}", source_slug(platform, source_id))
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
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, utoipa::ToSchema)]
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
    write_json(&out_dir.join(MANIFEST_FILE), manifest)
}

/// Pretty JSON with a trailing newline, the format of every JSON file steps write.
pub fn write_json<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    let mut text = serde_json::to_string_pretty(value).map_err(io::Error::other)?;
    text.push('\n');
    fs::write(path, text)
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

/// Finished output of an earlier step and its manifest. The executor starts a step only after
/// the steps before it succeeded, so a missing directory is permanent.
pub fn prior_step(job_dir: &Path, step: StepName) -> Result<(PathBuf, Manifest), StepFailure> {
    let dir = find_step(job_dir, step)?
        .ok_or_else(|| StepFailure::permanent(format!("{step} output is missing")))?;
    let manifest = read_manifest(&dir)?;
    Ok((dir, manifest))
}

/// Path of an earlier step's file by its logical name in `manifest.files`.
pub fn step_file(job_dir: &Path, step: StepName, name: &str) -> Result<PathBuf, StepFailure> {
    let (dir, manifest) = prior_step(job_dir, step)?;
    let rel = manifest
        .files
        .get(name)
        .ok_or_else(|| StepFailure::permanent(format!("{step} has no {name} file")))?;
    Ok(dir.join(rel))
}

/// Width of a section: video-frames groups text and keyframes into these windows, and translate
/// places Chinese subtitle cues by the same windows.
pub const SECTION_MS: u64 = 30_000;

/// One entry of video-frames' sections.json. `text` is the raw transcript; whoever writes it
/// into Markdown escapes it with [`escape_markdown`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Section {
    pub start_ms: u64,
    /// Basenames under video-frames' `assets/`.
    pub frames: Vec<String>,
    pub text: String,
}

impl Section {
    pub fn has_text(&self) -> bool {
        !self.text.trim().is_empty()
    }
}

pub fn read_sections(job_dir: &Path) -> Result<Vec<Section>, StepFailure> {
    let path = step_file(job_dir, StepName::VideoFrames, "sections")?;
    read_json(&path).map_err(|e| StepFailure::permanent(e.to_string()))
}

/// Section time in transcripts and notes: `MM:SS`, or `H:MM:SS` from one hour on.
pub fn clock(ms: u64) -> String {
    let s = ms / 1000;
    if s >= 3600 {
        format!("{}:{:02}:{:02}", s / 3600, s / 60 % 60, s % 60)
    } else {
        format!("{:02}:{:02}", s / 60, s % 60)
    }
}

/// Escapes plain transcript text so Obsidian shows it literally: emphasis, code, links, tags,
/// math, HTML, `%%` comments, and list or fence markers at the start of a line.
pub fn escape_markdown(text: &str) -> String {
    const SPECIAL: &[char] = &['\\', '`', '*', '_', '[', ']', '<', '>', '$', '#', '%'];
    let mut out = String::with_capacity(text.len());
    let mut line_start = true;
    for c in text.chars() {
        if SPECIAL.contains(&c) || (line_start && matches!(c, '-' | '+' | '~')) {
            out.push('\\');
        }
        out.push(c);
        line_start = c == '\n';
    }
    out
}

pub fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> io::Result<T> {
    let text = fs::read_to_string(path)?;
    serde_json::from_str(&text).map_err(|err| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("{}: {err}", path.display()),
        )
    })
}

/// A submitted link or bare arXiv id. The URL in the result is always rebuilt by the server.
/// Bilibili `?p=` is dropped (only P1 is processed); b23.tv needs a network lookup and is refused.
pub fn parse_source(input: &str) -> Option<Source> {
    let input = input.trim();
    let bare = input.strip_prefix("arXiv:").unwrap_or(input);
    if let Some(id) = arxiv_id(bare) {
        return Some(source(Platform::Arxiv, id));
    }
    let url = split_url(input)?;
    match (url.host.as_str(), url.segs.as_slice()) {
        ("bilibili.com", ["video", id]) => video_source(Platform::Bilibili, id),
        ("youtu.be", [id]) | ("youtube.com", ["shorts" | "embed" | "live", id]) => {
            video_source(Platform::Youtube, id)
        }
        ("youtube.com", ["watch"]) => video_source(Platform::Youtube, query(url.query, "v")?),
        ("arxiv.org", ["abs" | "pdf" | "html", rest @ ..]) => {
            let id = rest.join("/");
            arxiv_id(id.strip_suffix(".pdf").unwrap_or(&id)).map(|id| source(Platform::Arxiv, id))
        }
        _ => None,
    }
}

/// A feed pick or link ID after its shape check: `BV` + 10 alphanumerics, or an 11-character
/// YouTube id.
pub fn video_source(platform: Platform, id: &str) -> Option<Source> {
    let ok = match platform {
        Platform::Bilibili => id.len() == 12 && id.starts_with("BV") && token(&id[2..], b""),
        Platform::Youtube => id.len() == 11 && token(id, b"-_"),
        Platform::Arxiv => false,
    };
    ok.then(|| source(platform, id.to_owned()))
}

fn source(platform: Platform, id: String) -> Source {
    let url = match platform {
        Platform::Bilibili => format!("https://www.bilibili.com/video/{id}"),
        Platform::Youtube => format!("https://www.youtube.com/watch?v={id}"),
        Platform::Arxiv => format!("https://arxiv.org/abs/{id}"),
    };
    Source { platform, id, url }
}

/// arXiv id without its version: `2401.12345`, or old style `hep-th/9711200`, `math.GT/0309136`.
fn arxiv_id(s: &str) -> Option<String> {
    let base = match s.rsplit_once('v') {
        Some((base, version)) if !base.is_empty() && number(version) => base,
        _ => s,
    };
    let digits = |s: &str, lens: &[usize]| lens.contains(&s.len()) && number(s);
    let ok = match base.split_once('/') {
        None => base
            .split_once('.')
            .is_some_and(|(yymm, n)| digits(yymm, &[4]) && digits(n, &[4, 5])),
        Some((archive, n)) => {
            let (name, sub_ok) = match archive.split_once('.') {
                None => (archive, true),
                Some((name, sub)) => (
                    name,
                    sub.len() == 2 && sub.bytes().all(|b| b.is_ascii_uppercase()),
                ),
            };
            sub_ok
                && !name.is_empty()
                && name.bytes().all(|b| b.is_ascii_lowercase() || b == b'-')
                && digits(n, &[7])
        }
    };
    ok.then(|| base.to_owned())
}

fn number(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// A collection feed URL, normalized to the one form per kind that feed.py accepts. The table of
/// forms is in contract 3 (docs/contracts.md).
pub fn parse_feed(input: &str) -> Option<(FeedKind, String)> {
    let url = split_url(input)?;
    let lists = |mid: &str, sid: &str, kind| {
        let name = if kind == FeedKind::BiliSeason {
            "season"
        } else {
            "series"
        };
        number(sid).then(|| {
            (
                kind,
                format!("https://space.bilibili.com/{mid}/lists/{sid}?type={name}"),
            )
        })
    };
    let yt = |kind, path: String| Some((kind, format!("https://www.youtube.com/{path}")));
    // The favlist `fid` is the media id, so every form reduces to the one without a mid.
    let fav = |id: &str| {
        let url = format!("https://www.bilibili.com/medialist/detail/ml{id}");
        number(id).then_some((FeedKind::BiliFav, url))
    };
    match (url.host.as_str(), url.segs.as_slice()) {
        ("space.bilibili.com", [mid, rest @ ..]) if number(mid) => match rest {
            [] | ["video" | "upload" | "dynamic", ..] => Some((
                FeedKind::BiliUp,
                format!("https://space.bilibili.com/{mid}"),
            )),
            ["channel", "collectiondetail"] => {
                lists(mid, query(url.query, "sid")?, FeedKind::BiliSeason)
            }
            ["channel", "seriesdetail"] => {
                lists(mid, query(url.query, "sid")?, FeedKind::BiliSeries)
            }
            ["lists", sid] => match query(url.query, "type")? {
                "season" => lists(mid, sid, FeedKind::BiliSeason),
                "series" => lists(mid, sid, FeedKind::BiliSeries),
                _ => None,
            },
            ["favlist"] => fav(query(url.query, "fid")?),
            _ => None,
        },
        ("bilibili.com", ["medialist", "detail", ml] | ["list", ml]) => fav(ml.strip_prefix("ml")?),
        ("youtube.com", [handle, ..])
            if handle.len() > 1 && handle.starts_with('@') && token(&handle[1..], b"-_.%") =>
        {
            yt(FeedKind::YtChannel, format!("{handle}/videos"))
        }
        ("youtube.com", [kind @ ("channel" | "c" | "user"), name, ..]) if token(name, b"-_.%") => {
            yt(FeedKind::YtChannel, format!("{kind}/{name}/videos"))
        }
        ("youtube.com", ["playlist"]) => {
            let list = query(url.query, "list").filter(|l| token(l, b"-_"))?;
            yt(FeedKind::YtPlaylist, format!("playlist?list={list}"))
        }
        _ => None,
    }
}

/// The `<kind>` argument of `feed.py list`.
pub fn feed_kind_arg(kind: FeedKind) -> &'static str {
    match kind {
        FeedKind::BiliUp => "bili_up",
        FeedKind::BiliSeason => "bili_season",
        FeedKind::BiliSeries => "bili_series",
        FeedKind::BiliFav => "bili_fav",
        FeedKind::YtChannel => "yt_channel",
        FeedKind::YtPlaylist => "yt_playlist",
    }
}

struct Url<'a> {
    /// Lowercased, without a leading `www.` or `m.`.
    host: String,
    segs: Vec<&'a str>,
    query: &'a str,
}

fn split_url(input: &str) -> Option<Url<'_>> {
    let s = input.trim();
    let s = ["https://", "http://"]
        .iter()
        .find_map(|p| {
            s.get(..p.len())
                .filter(|h| h.eq_ignore_ascii_case(p))
                .map(|_| &s[p.len()..])
        })
        .unwrap_or(s);
    let s = s.split('#').next()?;
    let (s, query) = s.split_once('?').unwrap_or((s, ""));
    let (host, path) = s.split_once('/').unwrap_or((s, ""));
    let host = host.to_ascii_lowercase();
    let host = ["www.", "m."]
        .iter()
        .find_map(|p| host.strip_prefix(p))
        .unwrap_or(&host)
        .to_owned();
    let segs = path.split('/').filter(|s| !s.is_empty()).collect();
    Some(Url { host, segs, query })
}

fn query<'a>(query: &'a str, key: &str) -> Option<&'a str> {
    query
        .split('&')
        .find_map(|kv| kv.strip_prefix(key)?.strip_prefix('='))
        .filter(|v| !v.is_empty())
}

/// Non-empty ASCII alphanumerics plus the given extra bytes.
fn token(s: &str, extra: &[u8]) -> bool {
    !s.is_empty()
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || extra.contains(&b))
}

#[cfg(test)]
mod tests;
