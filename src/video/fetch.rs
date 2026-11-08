//! video-fetch: yt-dlp for both platforms, failure classification and subtitle choice.

use std::{
    collections::BTreeMap,
    env, fs, io,
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
};

use serde::Deserialize;
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::Command,
};

use super::{env_value, read_json, transcribe::parse_srt};
use crate::pipeline::{
    self, Manifest, MetaValue, Platform, StepFailure, StepResult, write_manifest,
};

pub(super) const VIDEO_FILE: &str = "video.mp4";
pub(super) const SUBTITLE_FILE: &str = "subtitle.srt";

/// Fields printed by `--print-to-file`. We never write `--write-info-json`: its format entries
/// carry the request cookies, which would put SESSDATA into the job directory.
#[derive(Debug, Deserialize)]
struct FetchedMeta {
    title: Option<String>,
    uploader: Option<String>,
    duration: Option<f64>,
    upload_date: Option<String>,
}

/// yt-dlp errors that will not change on retry. Everything else, including 412/429, bot checks
/// and network errors, is retried.
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
    "confirm your age",
    "unsupported url",
    "http error 404",
    "premieres in",
    "live event will begin",
];

pub async fn fetch(job_dir: &Path, out_dir: &Path) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let id = &job.source.id;
    let (url, proxy, sessdata) = match job.source.platform {
        Platform::Youtube => (
            format!("https://www.youtube.com/watch?v={id}"),
            env_value("HTTPS_PROXY").or_else(|| env_value("HTTP_PROXY")),
            None,
        ),
        // `?p=1` keeps multi-part videos to their first part. Bilibili never goes via the proxy.
        Platform::Bilibili => (
            format!("https://www.bilibili.com/video/{id}?p=1"),
            None,
            env_value("FLORI_BILI_SESSDATA"),
        ),
        Platform::Arxiv => return Err(StepFailure::permanent("video-fetch got an arxiv job")),
    };
    let meta_file = out_dir.join("fetched.json");
    let mut cmd = Command::new("yt-dlp");
    cmd.args(["--ignore-config", "--no-plugin-dirs", "--no-playlist"])
        .args(["--no-progress", "--no-colors", "--proxy"])
        .arg(proxy.unwrap_or_default())
        // h264 first: it plays in every browser and ffmpeg decodes it fast.
        .args(["-f", "bv*+ba/b", "-S", "vcodec:h264,res:1080,acodec:m4a"])
        .args(["--merge-output-format", "mp4", "--remux-video", "mp4"])
        // Manual subtitles only. Auto captions are worse than whisper.
        .args(["--write-subs", "--sub-langs", "zh.*,en.*"])
        .args(["--convert-subs", "srt"])
        .arg("-o")
        .arg(out_dir.join("video.%(ext)s"))
        .arg("-o")
        .arg(format!("subtitle:{}/subtitle.%(ext)s", out_dir.display()))
        .arg("--print-to-file")
        .arg("after_move:%(.{title,uploader,duration,upload_date})j")
        .arg(&meta_file);
    let cookies = match &sessdata {
        Some(value) => Some(write_cookies(job.job_id, value)?),
        None => None,
    };
    let downloaded = download(cmd, &url, cookies.as_deref()).await;
    if let Some(path) = &cookies {
        let _ = fs::remove_file(path);
    }
    downloaded?;
    if !out_dir.join(VIDEO_FILE).is_file() {
        return Err(StepFailure::permanent("yt-dlp finished without video.mp4"));
    }
    let fetched: FetchedMeta = read_json(&meta_file)?;
    fs::remove_file(&meta_file)?;

    let subtitle_language = keep_best_subtitle(out_dir)?;
    let mut files = BTreeMap::from([("video".into(), VIDEO_FILE.into())]);
    if subtitle_language.is_some() {
        files.insert("subtitle".into(), SUBTITLE_FILE.into());
    }
    let text = |key: &str, value: Option<String>| Some((key.to_owned(), MetaValue::Text(value?)));
    let duration_ms = |s: f64| {
        (
            "duration_ms".to_owned(),
            MetaValue::Int((s * 1000.0).round() as i64),
        )
    };
    let meta = [
        text("title", fetched.title.clone()),
        text("uploader", fetched.uploader),
        fetched.duration.map(duration_ms),
        text(
            "published",
            fetched.upload_date.as_deref().and_then(published_date),
        ),
        text("subtitle_language", subtitle_language),
    ];
    let manifest = Manifest {
        title: fetched.title,
        files,
        assets: Vec::new(),
        meta: meta.into_iter().flatten().collect(),
    };
    write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// Netscape cookie file for curl and yt-dlp in TMPDIR, never in the job directory. A Cookie
/// header would be scoped to www.bilibili.com only and would sit in the process arguments.
pub(super) fn write_cookies(job_id: i64, sessdata: &str) -> Result<PathBuf, StepFailure> {
    if sessdata.contains(|c: char| c.is_whitespace() || c.is_control()) {
        return Err(StepFailure::auth("FLORI_BILI_SESSDATA is malformed"));
    }
    let path = env::temp_dir().join(format!("flori-bili-{job_id}.cookies"));
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&path)?;
    writeln!(file, "# Netscape HTTP Cookie File")?;
    writeln!(
        file,
        ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\t{sessdata}"
    )?;
    Ok(path)
}

/// Split out of `fetch` so the cookie file is removed on every path.
async fn download(mut cmd: Command, url: &str, cookies: Option<&Path>) -> StepResult {
    if let Some(path) = cookies {
        check_login(path).await?;
        cmd.arg("--cookies").arg(path);
    }
    cmd.arg("--").arg(url);
    let (status, errors) = run_ytdlp(cmd).await?;
    classify_ytdlp(status.success(), &errors).map_or(Ok(()), Err)
}

/// An expired SESSDATA does not fail yt-dlp: it silently drops to preview quality and loses
/// subtitles. Bilibili's nav API is the one explicit answer, so ask it before downloading.
async fn check_login(cookies: &Path) -> StepResult {
    let output = Command::new("curl")
        .args(["-sS", "--max-time", "20", "--noproxy", "*", "-b"])
        .arg(cookies)
        .arg("https://api.bilibili.com/x/web-interface/nav")
        .stdin(Stdio::null())
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
/// ERROR lines for classification.
async fn run_ytdlp(mut cmd: Command) -> Result<(ExitStatus, Vec<String>), StepFailure> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stderr(Stdio::piped())
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
    Ok((child.wait().await?, errors))
}

/// `None` means the download is usable. The last ERROR line decides.
pub(super) fn classify_ytdlp(success: bool, errors: &[String]) -> Option<StepFailure> {
    if success {
        return None;
    }
    let Some(error) = errors.last() else {
        return Some(StepFailure::retry("yt-dlp failed without an ERROR line"));
    };
    let lower = error.to_lowercase();
    Some(if PERMANENT_ERRORS.iter().any(|p| lower.contains(p)) {
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

/// Keeps the best usable `subtitle.<lang>.srt` as `subtitle.srt`, deletes the rest and returns
/// its primary language subtag.
pub(super) fn keep_best_subtitle(out_dir: &Path) -> io::Result<Option<String>> {
    let mut found = Vec::new();
    for entry in fs::read_dir(out_dir)? {
        let name = entry?.file_name().to_string_lossy().into_owned();
        if let Some(lang) = name
            .strip_prefix("subtitle.")
            .and_then(|rest| rest.strip_suffix(".srt"))
        {
            found.push((subtitle_rank(lang), lang.to_owned(), out_dir.join(&name)));
        }
    }
    found.sort();
    let mut chosen = None;
    for (rank, lang, path) in found {
        if chosen.is_none() && rank.is_some() && !parse_srt(&fs::read_to_string(&path)?).is_empty()
        {
            fs::rename(&path, out_dir.join(SUBTITLE_FILE))?;
            let primary = lang.split('-').next().unwrap_or(&lang);
            chosen = Some(primary.to_ascii_lowercase());
        } else {
            fs::remove_file(&path)?;
        }
    }
    Ok(chosen)
}

/// Chinese first, simplified before traditional, then English.
pub(super) fn subtitle_rank(lang: &str) -> Option<u8> {
    let lang = lang.to_ascii_lowercase();
    match lang.as_str() {
        "zh" | "zh-hans" | "zh-cn" | "zh-sg" => Some(0),
        l if l.starts_with("zh-") => Some(1),
        l if l == "en" || l.starts_with("en-") => Some(2),
        _ => None,
    }
}
