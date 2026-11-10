//! video-fetch: yt-dlp for both platforms, failure classification and subtitle choice.

use std::{
    collections::BTreeMap,
    env,
    ffi::OsString,
    fs, io,
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
};

use serde::Deserialize;
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
    signal::unix::{SignalKind, signal},
};

use super::{env_value, exit_failure, transcribe::parse_srt};
use crate::pipeline::{
    self, Manifest, MetaValue, Platform, StepFailure, StepResult, read_json, write_manifest,
};

pub(super) const VIDEO_FILE: &str = "video.mp4";
pub(super) const SUBTITLE_FILE: &str = "subtitle.srt";
pub(super) const SUBTITLE_ZH_FILE: &str = "subtitle_zh.srt";
/// Logical name prefix of a Bilibili AI track, `ai_subtitle_<lang>`; its file adds `.srt`.
pub(super) const AI_SUBTITLE: &str = "ai_subtitle_";

/// Fields printed by `--print-to-file`. We never write `--write-info-json`: its format entries
/// carry the request cookies, which would put SESSDATA into the job directory.
#[derive(Debug, Deserialize)]
struct FetchedMeta {
    title: Option<String>,
    /// Set for a multi-part Bilibili video, whose `title` carries a ` pNN <part>` suffix.
    playlist_title: Option<String>,
    uploader: Option<String>,
    duration: Option<f64>,
    upload_date: Option<String>,
    /// The spoken language, e.g. `en` or `en-US`. yt-dlp's Bilibili extractor never sets it.
    language: Option<String>,
}

/// Throttling that yt-dlp words like a permanent error, e.g. YouTube's "Video unavailable. This
/// content isn't available, try again later". Checked before [`PERMANENT_ERRORS`].
const RETRY_ERRORS: &[&str] = &[
    "try again later",
    "rate-limit",
    "rate limit",
    "not a bot",
    "http error 429",
    "http error 412",
];

/// yt-dlp errors that will not change on retry. Anything in neither list is retried.
const PERMANENT_ERRORS: &[&str] = &[
    "video unavailable",
    "this video is unavailable",
    "private video",
    "deleted or geo-restricted",
    "available in your country",
    "geo restriction",
    "has been removed",
    "been terminated",
    "members-only",
    "premium members only",
    "only available for registered users",
    "confirm your age",
    "unsupported url",
    "http error 404",
    "premieres in",
    "live event will begin",
];

/// What video-fetch takes from its environment. Tests point the tools at fakes.
pub(super) struct FetchEnv {
    pub(super) proxy: Option<String>,
    pub(super) sessdata: Option<String>,
    pub(super) tmp_dir: PathBuf,
    pub(super) ytdlp: OsString,
    pub(super) curl: OsString,
}

impl FetchEnv {
    fn inherited() -> FetchEnv {
        FetchEnv {
            proxy: env_value("HTTPS_PROXY").or_else(|| env_value("HTTP_PROXY")),
            sessdata: env_value("FLORI_BILI_SESSDATA"),
            tmp_dir: env::temp_dir(),
            ytdlp: "yt-dlp".into(),
            curl: "curl".into(),
        }
    }
}

/// SIGTERM (cancel or step timeout) goes to the whole process group. If it lands while a tool
/// runs, the step exits 10 without a manifest and with the cookie removed. `biased` polls the
/// signal first, so once tokio reports it the download future is dropped: tools spawned with
/// `kill_on_drop` die and [`CookieFile`] removes the cookie. tokio can report the signal a
/// wakeup after the death of a tool killed by the same SIGTERM, so the download may see that
/// death first. [`run_ytdlp`] and [`check_login`] retry it, so the step still exits 10 and the
/// cookie goes when `fetch_with` returns.
pub async fn fetch(job_dir: &Path, out_dir: &Path) -> StepResult {
    let mut term = signal(SignalKind::terminate())
        .map_err(|e| StepFailure::retry(format!("install SIGTERM handler: {e}")))?;
    let env = FetchEnv::inherited();
    tokio::select! {
        biased;
        _ = term.recv() => Err(StepFailure::retry("stopped by SIGTERM")),
        result = fetch_with(job_dir, out_dir, &env) => result,
    }
}

pub(super) async fn fetch_with(job_dir: &Path, out_dir: &Path, env: &FetchEnv) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let id = &job.source.id;
    // A multi-part Bilibili video is a playlist; `--playlist-items 1` keeps its first part.
    let (url, proxy, sessdata) = match job.source.platform {
        Platform::Youtube => (
            format!("https://www.youtube.com/watch?v={id}"),
            env.proxy.as_deref(),
            None,
        ),
        // Bilibili never goes via the proxy.
        Platform::Bilibili => (
            format!("https://www.bilibili.com/video/{id}"),
            None,
            env.sessdata.as_deref(),
        ),
        Platform::Arxiv => return Err(StepFailure::permanent("video-fetch got an arxiv job")),
    };
    let cookies = match sessdata {
        Some(value) => Some(CookieFile::write(&env.tmp_dir, job.job_id, value)?),
        None => None,
    };
    let cookie_path = cookies.as_ref().map(|c| c.0.as_path());
    if let Some(path) = cookie_path {
        check_login(env, path).await?;
    }
    // Both runs share the output template, so yt-dlp's intermediate files land in out_dir.
    let ytdlp = || {
        let mut cmd = Command::new(&env.ytdlp);
        cmd.args(["--ignore-config", "--no-plugin-dirs"])
            .args(["--playlist-items", "1", "--no-progress", "--no-colors"])
            .arg("--proxy")
            .arg(proxy.unwrap_or_default())
            // A running live stream would be recorded until the step times out.
            .args(["--match-filter", "!is_live", "-o"])
            .arg(out_dir.join("video.%(ext)s"));
        if let Some(path) = cookie_path {
            cmd.arg("--cookies").arg(path);
        }
        cmd
    };

    let meta_file = out_dir.join("fetched.json");
    let mut video = ytdlp();
    // h264 first: it plays in every browser and ffmpeg decodes it fast.
    video
        // 720p is enough for keyframes and playback, and the YouTube path goes through a
        // proxy that can drop to ~100KB/s; 1080p then runs into the 30m step timeout.
        .args(["-f", "bv*+ba/b", "-S", "vcodec:h264,res:720,acodec:m4a"])
        .args(["--merge-output-format", "mp4", "--remux-video", "mp4"])
        .arg("--print-to-file")
        .arg("after_move:%(.{title,playlist_title,uploader,duration,upload_date,language})j")
        .arg(&meta_file)
        .arg("--")
        .arg(&url);
    let (status, errors) = run_ytdlp(video).await?;
    if let Some(failure) = classify_ytdlp(status.success(), &errors) {
        return Err(failure);
    }
    if !out_dir.join(VIDEO_FILE).is_file() {
        return Err(StepFailure::permanent(
            "yt-dlp exited 0 without video.mp4; a live stream still running is skipped",
        ));
    }
    let fetched: FetchedMeta = read_json(&meta_file)?;
    fs::remove_file(&meta_file)?;
    // The language becomes a --sub-langs regex, so anything but a plain tag counts as unknown.
    // yt-dlp's Bilibili extractor never sets it; the unknown order takes Chinese first.
    let plain_tag = |o: &String| o.len() >= 2 && o.bytes().all(|b| b.is_ascii_lowercase());
    let original = fetched.language.as_deref().map(primary).filter(plain_tag);
    // Only tracks keep_subtitles can use: a third original language never needs English, and
    // one failed language request aborts the whole subtitle run.
    let langs = match original.as_deref() {
        Some(o) if o != "zh" && o != "en" => vec![o, "zh"],
        _ => vec!["zh", "en"],
    };
    let mut sub_langs: Vec<String> = langs.iter().map(|l| format!("{l}.*")).collect();
    // Bilibili's AI tracks `ai-<lang>` come with the subtitle list it already fetches, so asking
    // for them costs no extra request. Bilibili lists them only to a logged-in SESSDATA.
    let ai_langs = match job.source.platform {
        Platform::Bilibili => langs.as_slice(),
        _ => &[],
    };
    sub_langs.extend(ai_langs.iter().map(|l| format!("ai-{l}")));

    // Subtitles are best effort in a second run: a failed subtitle request (429 from YouTube's
    // timedtext, Bilibili's subtitle JSON) must not cost the video; whisper covers it.
    // Manual subtitles and Bilibili AI tracks only. YouTube auto captions are worse than whisper.
    let mut subtitles = ytdlp();
    subtitles
        .args(["--skip-download", "--write-subs"])
        .args([
            "--sub-langs",
            &sub_langs.join(","),
            "--convert-subs",
            "srt",
            "-o",
        ])
        .arg(format!("subtitle:{}/subtitle.%(ext)s", out_dir.display()))
        .arg("--")
        .arg(&url);
    let (status, _) = run_ytdlp(subtitles).await?;
    if !status.success() {
        eprintln!("video-fetch: subtitle download failed, video-transcribe will use whisper");
    }
    drop(cookies);

    let kept = keep_subtitles(out_dir, original.as_deref(), ai_langs)?;
    let mut files = BTreeMap::from([("video".into(), VIDEO_FILE.into())]);
    if kept.language.is_some() {
        files.insert("subtitle".into(), SUBTITLE_FILE.into());
    }
    if kept.chinese {
        files.insert("subtitle_zh".into(), SUBTITLE_ZH_FILE.into());
    }
    for lang in &kept.ai {
        let name = format!("{AI_SUBTITLE}{lang}");
        files.insert(name.clone(), format!("{name}.srt"));
    }
    let title = fetched.playlist_title.or(fetched.title);
    let text = |key: &str, value: Option<String>| Some((key.to_owned(), MetaValue::Text(value?)));
    let duration_ms = |s: f64| {
        (
            "duration_ms".to_owned(),
            MetaValue::Int((s * 1000.0).round() as i64),
        )
    };
    let meta = [
        text("title", title.clone()),
        text("uploader", fetched.uploader),
        fetched.duration.map(duration_ms),
        text(
            "published",
            fetched.upload_date.as_deref().and_then(published_date),
        ),
        text("subtitle_language", kept.language),
    ];
    let manifest = Manifest {
        title,
        files,
        assets: Vec::new(),
        meta: meta.into_iter().flatten().collect(),
    };
    write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// Netscape cookie file for curl and yt-dlp in TMPDIR, never in the job directory, removed on
/// drop. A Cookie header would be scoped to www.bilibili.com only and sit in the process
/// arguments.
pub(super) struct CookieFile(pub(super) PathBuf);

impl CookieFile {
    pub(super) fn write(tmp_dir: &Path, job_id: i64, sessdata: &str) -> Result<Self, StepFailure> {
        if sessdata.contains(|c: char| c.is_whitespace() || c.is_control()) {
            return Err(StepFailure::auth("FLORI_BILI_SESSDATA is malformed"));
        }
        let path = tmp_dir.join(format!("flori-bili-{job_id}.cookies"));
        // A leftover from a killed attempt, or a planted symlink, goes first; create_new then
        // refuses anything that reappears in between.
        match fs::remove_file(&path) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => return Err(e.into()),
            _ => {}
        }
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        let cookie = CookieFile(path);
        writeln!(file, "# Netscape HTTP Cookie File")?;
        writeln!(
            file,
            ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t{sessdata}"
        )?;
        Ok(cookie)
    }
}

impl Drop for CookieFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

/// An expired SESSDATA does not fail yt-dlp: it silently drops to preview quality and loses
/// subtitles. Bilibili's nav API is the one explicit answer, so ask it before downloading.
async fn check_login(env: &FetchEnv, cookies: &Path) -> StepResult {
    let output = Command::new(&env.curl)
        .args(["-sS", "--max-time", "20", "--noproxy", "*", "-b"])
        .arg(cookies)
        .arg("https://api.bilibili.com/x/web-interface/nav")
        .stdin(Stdio::null())
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|e| StepFailure::permanent(format!("curl: {e}")))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(StepFailure::retry(format!(
            "bilibili login check: {}",
            stderr.trim()
        )));
    }
    login_state(&output.stdout)
}

#[derive(Debug, Deserialize)]
struct NavReply {
    code: i64,
}

/// Code -101 is "账号未登录". Anything else unexpected, such as risk control, is retried.
pub(super) fn login_state(body: &[u8]) -> StepResult {
    match serde_json::from_slice::<NavReply>(body).map(|reply| reply.code) {
        Ok(0) => Ok(()),
        Ok(-101) => Err(StepFailure::auth(
            "bilibili reports FLORI_BILI_SESSDATA as not logged in",
        )),
        Ok(code) => Err(StepFailure::retry(format!(
            "bilibili login check returned code {code}"
        ))),
        Err(_) => Err(StepFailure::retry("bilibili login check returned no JSON")),
    }
}

/// Runs yt-dlp with stdout inherited and stderr echoed line by line into the job log. Returns its
/// ERROR lines for classification. A yt-dlp killed by a signal is retried before any
/// classification: it is usually the executor's SIGTERM, and a subtitle run that died of it must
/// not pass for "no subtitles" and let the step exit 0.
async fn run_ytdlp(mut cmd: Command) -> Result<(ExitStatus, Vec<String>), StepFailure> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| StepFailure::permanent(format!("yt-dlp: {e}")))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| io::Error::other("no stderr"))?;
    let mut stderr = BufReader::new(stderr);
    let mut errors = Vec::new();
    let mut line = Vec::new();
    while stderr.read_until(b'\n', &mut line).await? > 0 {
        let text = String::from_utf8_lossy(&line);
        let text = text.trim_end();
        eprintln!("{text}");
        if text.starts_with("ERROR:") && errors.len() < 100 {
            errors.push(text.to_owned());
        }
        line.clear();
    }
    let status = child.wait().await?;
    if status.code().is_none() {
        return Err(exit_failure("yt-dlp", status));
    }
    Ok((status, errors))
}

/// `None` means the download is usable. The last ERROR line decides: throttling first, then
/// known permanent causes, anything else is retried.
pub(super) fn classify_ytdlp(success: bool, errors: &[String]) -> Option<StepFailure> {
    if success {
        return None;
    }
    let Some(error) = errors.last() else {
        return Some(StepFailure::retry("yt-dlp failed without an ERROR line"));
    };
    let lower = error.to_lowercase();
    let matches = |patterns: &[&str]| patterns.iter().any(|p| lower.contains(p));
    Some(if !matches(RETRY_ERRORS) && matches(PERMANENT_ERRORS) {
        StepFailure::permanent(error.clone())
    } else {
        StepFailure::retry(error.clone())
    })
}

/// `YYYYMMDD` from yt-dlp to `YYYY-MM-DD`.
pub(super) fn published_date(value: &str) -> Option<String> {
    (value.len() == 8 && value.bytes().all(|b| b.is_ascii_digit()))
        .then(|| format!("{}-{}-{}", &value[..4], &value[4..6], &value[6..]))
}

/// A usable downloaded `subtitle.<lang>.srt`.
struct Track {
    rank: (u8, u8),
    /// Lowercased primary subtag, e.g. `zh` for `zh-Hans`.
    primary: String,
    path: PathBuf,
}

/// What [`keep_subtitles`] left in out_dir.
#[derive(Debug, PartialEq)]
pub(super) struct Kept {
    /// Primary subtag of `subtitle.srt`, if written.
    pub(super) language: Option<String>,
    /// Whether `subtitle_zh.srt` was written.
    pub(super) chinese: bool,
    /// Languages of the `ai_subtitle_<lang>.srt` files written.
    pub(super) ai: Vec<String>,
}

/// Keeps the best usable manual track as `subtitle.srt` (see [`subtitle_rank`]) and, when the
/// original is known and not Chinese, the best Chinese manual track as `subtitle_zh.srt`: a
/// human translation translate uses instead of the AI. With the original unknown a Chinese track
/// may be the original itself, so it is never taken for a translation.
///
/// Bilibili AI tracks `ai-<lang>` for a language in `ai_langs` are kept as
/// `ai_subtitle_<lang>.srt` only when no manual track made `subtitle.srt`. Each is speech
/// recognition when `<lang>` is the spoken language and a machine translation otherwise, and
/// Bilibili does not say which, so video-transcribe confirms the language from the audio first.
/// They never become `subtitle.srt` or `subtitle_zh.srt`.
///
/// Everything else except video.mp4, such as other languages or an unconverted `.vtt` left by a
/// failed subtitle run, is deleted.
pub(super) fn keep_subtitles(
    out_dir: &Path,
    original: Option<&str>,
    ai_langs: &[&str],
) -> io::Result<Kept> {
    let mut tracks = Vec::new();
    let mut ai = Vec::new();
    for entry in fs::read_dir(out_dir)? {
        let path = entry?.path();
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let lang = name
            .strip_prefix("subtitle.")
            .and_then(|l| l.strip_suffix(".srt"));
        let ai_lang = lang
            .and_then(|l| l.strip_prefix("ai-"))
            .filter(|l| ai_langs.contains(l));
        match (lang, ai_lang) {
            (Some(_), _) if parse_srt(&fs::read_to_string(&path)?).is_empty() => {
                fs::remove_file(&path)?;
            }
            (Some(_), Some(ai_lang)) => ai.push((ai_lang.to_owned(), path.clone())),
            (Some(lang), None) => {
                tracks.push(Track {
                    rank: subtitle_rank(lang, original),
                    primary: primary(lang),
                    path: path.clone(),
                });
            }
            (None, _) if name == VIDEO_FILE => {}
            (None, _) => fs::remove_file(&path)?,
        }
    }
    tracks.sort_by(|a, b| (a.rank, &a.path).cmp(&(b.rank, &b.path)));
    let main = tracks.first().filter(|t| t.rank.0 < u8::MAX);
    let translated = original.is_some_and(|o| o != "zh");
    let chinese = tracks
        .iter()
        .find(|t| t.primary == "zh")
        .filter(|_| translated);
    for (file, track) in [(SUBTITLE_FILE, main), (SUBTITLE_ZH_FILE, chinese)] {
        if let Some(track) = track {
            fs::rename(&track.path, out_dir.join(file))?;
        }
    }
    for track in tracks.iter().filter(|t| t.path.exists()) {
        fs::remove_file(&track.path)?;
    }
    if main.is_some() {
        for (_, path) in ai.drain(..) {
            fs::remove_file(path)?;
        }
    }
    ai.sort();
    for (lang, path) in &ai {
        fs::rename(path, out_dir.join(format!("{AI_SUBTITLE}{lang}.srt")))?;
    }
    Ok(Kept {
        language: main.map(|t| t.primary.clone()),
        chinese: chinese.is_some(),
        ai: ai.into_iter().map(|(lang, _)| lang).collect(),
    })
}

/// Lowercased primary subtag: `en-US` is `en`, `zh-Hans` is `zh`.
fn primary(lang: &str) -> String {
    lang.split('-').next().unwrap_or(lang).to_ascii_lowercase()
}

/// Sort key of a track for `subtitle.srt`; `u8::MAX` is never chosen. A known original takes
/// only its own language, except that a Chinese original falls back to English. An unknown
/// original takes Chinese, then English: YouTube leaves the language unset mostly on videos
/// without speech recognition, which are often Chinese. Within a language, simplified Chinese
/// before traditional, a bare tag before regional ones.
pub(super) fn subtitle_rank(lang: &str, original: Option<&str>) -> (u8, u8) {
    let lang = lang.to_ascii_lowercase();
    let main = match (original, primary(&lang).as_str()) {
        (Some(o), p) if p == o => 0,
        (None, "zh") => 1,
        (Some("zh") | None, "en") => 2,
        _ => u8::MAX,
    };
    let variant = match lang.as_str() {
        "zh-hans" | "zh-cn" | "zh-sg" => 0,
        l => u8::from(l.contains('-')),
    };
    (main, variant)
}
