//! claude and codex adapters behind one `complete` call, failure classification, and the note step.
//! The fixed CLI arguments are contract 1 "AI CLI 固定调用" in docs/contracts.md.

use std::{
    ffi::OsString,
    fs,
    future::Future,
    io,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::atomic::{AtomicU32, Ordering},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use rustix::process::{Pid, Signal, kill_process_group};
use serde::Deserialize;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
    signal::unix::{SignalKind, signal},
};

use crate::pipeline::{self, AiTool, Manifest, Pipeline, StepFailure, StepName, StepResult};

/// Separates instructions from untrusted input. tests/fake echoes what follows the last one.
pub const INPUT_MARKER: &str = "---FLORI-INPUT---";
/// Fixed flags besides model, paths and the empty tool list.
const CLAUDE_FLAGS: &str = "--output-format json --max-turns 1 --no-session-persistence \
    --strict-mcp-config --setting-sources project";
/// Order matters: `--ask-for-approval` belongs to codex itself, the rest to `exec`.
const CODEX_FLAGS: &str = "--ask-for-approval never exec --strict-config --ignore-user-config \
    --ignore-rules --skip-git-repo-check --sandbox read-only --ephemeral --color never --json";
const NOTE_TIMEOUT: Duration = Duration::from_secs(1800);
/// After SIGTERM on timeout the CLI gets this long before its group is killed.
const TERM_GRACE: Duration = Duration::from_secs(10);
const NOTE_ARXIV: &str = include_str!("../prompts/note-arxiv.md");
const NOTE_VIDEO: &str = include_str!("../prompts/note-video.md");

/// Matched against the lowercased failure text, auth first. Anything unmatched is permanent (20),
/// including error_max_turns and spend limits.
const AUTH_MARKERS: &[&str] = &[
    "not logged in",
    "login expired",
    "authentication required",
    "oauth token",
    "failed to authenticate",
    "api error: 401",
    "does not have access",
    "please run /login",
    "401 unauthorized",
    "token expired",
    // codex: "Your access token could not be refreshed because your refresh token has expired
    // (or was already used, or was revoked). Please log out and sign in again."
    "refresh token",
    "sign in again",
];
const LIMIT_MARKERS: &[&str] = &[
    "you've hit your",
    "usage limit reached",
    "request rejected (429)",
    "temporarily limiting",
    "repeated 529",
    "high load",
    "429 too many requests",
    "rate limit",
];

/// Environment handed to the AI CLI. The executor already whitelisted the step's environment,
/// so production passes it through unchanged (`inherited`); nothing is added here.
pub type Env = [(OsString, OsString)];

pub fn inherited() -> Vec<(OsString, OsString)> {
    std::env::vars_os().collect()
}

pub async fn note(job_dir: &Path, out_dir: &Path) -> StepResult {
    until_terminated(note_with(job_dir, out_dir, &inherited())).await
}

pub(crate) async fn note_with(job_dir: &Path, out_dir: &Path, env: &Env) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let (template, fetch, text) = match job.pipeline {
        Pipeline::Arxiv => {
            let original = pipeline::step_file(job_dir, StepName::ArxivConvert, "original")?;
            (
                NOTE_ARXIV,
                StepName::ArxivFetch,
                fs::read_to_string(original)?,
            )
        }
        // Sections carry the transcript without image links, with times the note can cite.
        Pipeline::Video => {
            let lines: Vec<String> = pipeline::read_sections(job_dir)?
                .iter()
                .filter(|s| s.has_text())
                .map(|s| format!("[{}] {}", pipeline::clock(s.start_ms), s.text.trim()))
                .collect();
            (NOTE_VIDEO, StepName::VideoFetch, lines.join("\n"))
        }
    };
    let title = pipeline::prior_step(job_dir, fetch)?.1.title;
    let title = title.unwrap_or_else(|| job.source.id.clone());
    let prompt = format!("{template}\n{INPUT_MARKER}\n标题:{title}\n\n{text}");
    let mut note = complete(env, job.ai.tool, &job.ai.model_note, &prompt, NOTE_TIMEOUT).await?;
    if !note.ends_with('\n') {
        note.push('\n');
    }
    fs::write(out_dir.join("note.md"), note)?;
    let mut manifest = Manifest::default();
    manifest.files.insert("note".into(), "note.md".into());
    pipeline::write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// Runs an AI step until it finishes or the executor sends SIGTERM (cancel or step timeout).
/// Dropping the step future drops the running CLI, whose `Group` guard kills its process group;
/// without this the CLI, in its own group, would outlive the step and keep spending quota.
pub(crate) async fn until_terminated(step: impl Future<Output = StepResult>) -> StepResult {
    let mut term = signal(SignalKind::terminate())
        .map_err(|e| StepFailure::retry(format!("install SIGTERM handler: {e}")))?;
    tokio::select! {
        result = step => result,
        _ = term.recv() => Err(StepFailure::retry("stopped by SIGTERM; AI CLI killed")),
    }
}

/// One single-turn completion without tools. The prompt goes to stdin and the CLI runs in a
/// fresh empty directory. Failures map to step exits: credentials 30, rate limit and timeout 10,
/// anything else 20.
pub async fn complete(
    env: &Env,
    tool: AiTool,
    model: &str,
    prompt: &str,
    timeout: Duration,
) -> Result<String, StepFailure> {
    // Without --model the CLI silently uses the host user's default model.
    if model.trim().is_empty() {
        return Err(StepFailure::permanent("AI model is not configured"));
    }
    let scratch = Scratch::new()?;
    let mut cwd = scratch.0.join("cwd");
    let last_message = scratch.0.join("last-message.md");
    let (name, mut cmd) = match tool {
        AiTool::Claude => {
            cwd = claude_cwd(env)?;
            let mut cmd = Command::new("claude");
            cmd.args(["-p", "--model", model, "--tools", ""])
                .args(CLAUDE_FLAGS.split_whitespace());
            ("claude", cmd)
        }
        AiTool::Codex => {
            let mut cmd = Command::new("codex");
            cmd.args(CODEX_FLAGS.split_whitespace())
                .args(["--model", model, "-c", "history.persistence=\"none\""])
                .args(["-c", "web_search=\"disabled\"", "-C"])
                .arg(&cwd)
                .arg("--output-last-message")
                .arg(&last_message)
                .arg("-");
            ("codex", cmd)
        }
    };
    cmd.current_dir(&cwd)
        .env_clear()
        .envs(env.iter().map(|(k, v)| (k, v)));
    let run = run_cli(cmd, prompt, timeout, TERM_GRACE)
        .await
        .map_err(|e| StepFailure::permanent(format!("{name}: {e}")))?;
    let Some(status) = run.status else {
        let secs = timeout.as_secs();
        return Err(StepFailure::retry(format!(
            "{name} timed out after {secs}s"
        )));
    };
    match tool {
        AiTool::Claude => claude_result(status, &run.stdout, &run.stderr),
        AiTool::Codex => {
            let text = fs::read_to_string(&last_message).unwrap_or_default();
            if status.success() && !text.trim().is_empty() {
                return Ok(text);
            }
            let detail = if status.success() {
                "empty last message".to_owned()
            } else {
                codex_errors(&run.stdout)
            };
            Err(classify(
                "codex",
                status,
                &format!("{detail} {}", run.stderr),
            ))
        }
    }
}

#[derive(Deserialize)]
struct ClaudeEnvelope {
    subtype: Option<String>,
    is_error: Option<bool>,
    result: Option<String>,
}

/// Success needs exit 0, is_error=false, subtype=success and a non-empty result. A failed run
/// still prints the envelope, and its result is what tells auth from rate limit.
fn claude_result(status: ExitStatus, stdout: &str, stderr: &str) -> Result<String, StepFailure> {
    let detail = match serde_json::from_str::<ClaudeEnvelope>(stdout.trim()) {
        Ok(envelope) => {
            let result = envelope.result.unwrap_or_default();
            let subtype = envelope.subtype.unwrap_or_default();
            if status.success()
                && envelope.is_error == Some(false)
                && subtype == "success"
                && !result.trim().is_empty()
            {
                return Ok(result);
            }
            format!("subtype={subtype} result={result}")
        }
        Err(_) => stdout.to_owned(),
    };
    Err(classify("claude", status, &format!("{detail} {stderr}")))
}

#[derive(Deserialize)]
struct CodexEvent {
    message: Option<String>,
    error: Option<CodexError>,
}

#[derive(Deserialize)]
struct CodexError {
    message: String,
}

/// Only error messages from the JSONL events; agent text is not matched against the markers.
fn codex_errors(stdout: &str) -> String {
    let mut messages = Vec::new();
    for line in stdout.lines() {
        if let Ok(event) = serde_json::from_str::<CodexEvent>(line) {
            messages.extend(event.message);
            messages.extend(event.error.map(|e| e.message));
        }
    }
    messages.join("; ")
}

fn classify(name: &str, status: ExitStatus, detail: &str) -> StepFailure {
    let detail = detail.split_whitespace().collect::<Vec<_>>().join(" ");
    let lower = detail.to_lowercase();
    let short: String = detail.chars().take(500).collect();
    let message = format!("{name} failed ({status}): {short}");
    if AUTH_MARKERS.iter().any(|m| lower.contains(m)) {
        StepFailure::auth(message)
    } else if LIMIT_MARKERS.iter().any(|m| lower.contains(m)) {
        StepFailure::retry(message)
    } else {
        StepFailure::permanent(message)
    }
}

struct Run {
    /// None when the timeout fired and the group was killed.
    status: Option<ExitStatus>,
    stdout: String,
    stderr: String,
}

/// Kills the CLI's whole process group when dropped: after a normal exit (stragglers would keep
/// the pipes open), after a timeout, and when the step future is dropped on SIGTERM.
struct Group(Pid);

impl Group {
    fn signal(&self, signal: Signal) {
        // ESRCH only means the group is already gone.
        let _ = kill_process_group(self.0, signal);
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.signal(Signal::KILL);
    }
}

/// Starts `cmd` in its own process group with `input` on stdin. On timeout the group gets
/// SIGTERM, then SIGKILL after `grace`.
async fn run_cli(
    mut cmd: Command,
    input: &str,
    timeout: Duration,
    grace: Duration,
) -> io::Result<Run> {
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true);
    let mut child = cmd.spawn()?;
    let pid = child.id().and_then(|id| i32::try_from(id).ok());
    let group = Group(
        pid.and_then(Pid::from_raw)
            .ok_or_else(|| io::Error::other("child exited before it had a pid"))?,
    );
    let mut stdin = child.stdin.take();
    let input = input.as_bytes().to_vec();
    // A CLI that exits without reading stdin causes EPIPE here; its exit status tells the story.
    let writer = tokio::spawn(async move {
        if let Some(stdin) = stdin.as_mut() {
            let _ = stdin.write_all(&input).await;
        }
    });
    let stdout = tokio::spawn(read_all(child.stdout.take()));
    let stderr = tokio::spawn(read_all(child.stderr.take()));
    let status = match tokio::time::timeout(timeout, child.wait()).await {
        Ok(status) => Some(status?),
        Err(_) => {
            group.signal(Signal::TERM);
            if tokio::time::timeout(grace, child.wait()).await.is_err() {
                group.signal(Signal::KILL);
                child.wait().await?;
            }
            None
        }
    };
    drop(group);
    writer.abort();
    Ok(Run {
        status,
        stdout: stdout.await.map_err(io::Error::other)?,
        stderr: stderr.await.map_err(io::Error::other)?,
    })
}

async fn read_all(pipe: Option<impl AsyncRead + Unpin>) -> String {
    let mut buf = Vec::new();
    if let Some(mut pipe) = pipe {
        let _ = pipe.read_to_end(&mut buf).await;
    }
    String::from_utf8_lossy(&buf).into_owned()
}

/// claude creates `~/.claude/projects/<cwd>/` for every working directory it runs in, even with
/// --no-session-persistence, and that directory is shared with the host CLI. A fixed cwd under
/// HOME keeps it to one entry; it is recreated empty each call so no project settings can appear.
/// AI steps never overlap (ai Semaphore(1)), so the fixed path is not contended.
fn claude_cwd(env: &Env) -> io::Result<PathBuf> {
    let home = env
        .iter()
        .find(|(k, _)| k == "HOME")
        .map(|(_, v)| PathBuf::from(v))
        .ok_or_else(|| io::Error::other("HOME is not set for the AI CLI"))?;
    let cwd = home.join("ai-cwd");
    match fs::remove_dir_all(&cwd) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::NotFound => {}
        Err(e) => return Err(e),
    }
    fs::create_dir_all(&cwd)?;
    Ok(cwd)
}

/// Per-call scratch directory under TMPDIR: `cwd/` stays empty, codex writes its reply beside it.
struct Scratch(PathBuf);

impl Scratch {
    fn new() -> io::Result<Scratch> {
        static SEQ: AtomicU32 = AtomicU32::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.subsec_nanos());
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let dir =
            std::env::temp_dir().join(format!("flori-ai-{}-{seq}-{nanos}", std::process::id()));
        // create_dir fails on an existing path, so a call never reuses another call's files.
        fs::create_dir(&dir)?;
        let scratch = Scratch(dir);
        fs::create_dir(scratch.0.join("cwd"))?;
        Ok(scratch)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
pub(crate) mod tests;
