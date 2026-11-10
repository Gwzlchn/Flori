//! claude and codex adapters behind one `complete` call and failure classification. The note
//! step lives in `note`. The fixed CLI arguments are contract 1 "AI CLI 固定调用" in
//! docs/contracts.md.

mod note;

use std::{
    ffi::OsString,
    fs,
    future::Future,
    io,
    path::PathBuf,
    process::{ExitStatus, Stdio},
    sync::atomic::{AtomicU32, Ordering},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use base64::{Engine, engine::general_purpose::STANDARD as BASE64};
use rustix::process::{Pid, Signal, kill_process_group};
use serde::Deserialize;
use serde_json::json;
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
    signal::unix::{SignalKind, signal},
};

use crate::pipeline::{AiTool, StepFailure, StepResult};

pub use note::note;

/// Separates instructions from untrusted input. tests/fake echoes what follows the last one.
pub const INPUT_MARKER: &str = "---FLORI-INPUT---";
/// Fixed flags besides model and the empty tool list. stream-json input lets one user message
/// carry the prompt and the images; stream-json output then needs --verbose.
const CLAUDE_FLAGS: &str = "--input-format stream-json --output-format stream-json --verbose \
    --max-turns 1 --no-session-persistence --strict-mcp-config --setting-sources project";
/// Order matters: `--ask-for-approval` belongs to codex itself, the rest to `exec`.
const CODEX_FLAGS: &str = "--ask-for-approval never exec --strict-config --ignore-user-config \
    --ignore-rules --skip-git-repo-check --sandbox read-only --ephemeral --color never --json";
/// After SIGTERM on timeout the CLI gets this long before its group is killed.
const TERM_GRACE: Duration = Duration::from_secs(10);

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
    // Error code on claude's stream-json events.
    "authentication_failed",
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
    "rate_limit",
];

/// Environment handed to the AI CLI. The executor already whitelisted the step's environment,
/// so production passes it through unchanged (`inherited`); nothing is added here.
pub type Env = [(OsString, OsString)];

pub fn inherited() -> Vec<(OsString, OsString)> {
    std::env::vars_os().collect()
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

/// One single-turn completion without tools, with optional images (see [`media_type`]). The
/// prompt goes to stdin and the CLI runs in an empty directory. Failures map to step exits:
/// credentials 30, rate limit and timeout 10, anything else 20.
pub async fn complete(
    env: &Env,
    tool: AiTool,
    model: &str,
    prompt: &str,
    images: &[PathBuf],
    timeout: Duration,
) -> Result<String, StepFailure> {
    // Without --model the CLI silently uses the host user's default model.
    if model.trim().is_empty() {
        return Err(StepFailure::permanent("AI model is not configured"));
    }
    let input = match tool {
        AiTool::Claude => claude_message(prompt, images)?,
        AiTool::Codex => prompt.to_owned(),
    };
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
            cmd.args(CODEX_FLAGS.split_whitespace());
            // `--image` takes several values, so each one is followed by another flag, never by
            // the final `-`.
            for image in images {
                cmd.arg("--image").arg(image);
            }
            cmd.args(["--model", model, "-c", "history.persistence=\"none\""])
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
    // One line per call in job.log: the only record of how many AI calls a job made.
    eprintln!(
        "{name} --model {model}: {} prompt chars, {} images",
        prompt.chars().count(),
        images.len()
    );
    let started = Instant::now();
    let run = run_cli(cmd, &input, timeout, TERM_GRACE)
        .await
        .map_err(|e| StepFailure::permanent(format!("{name}: {e}")))?;
    let Some(status) = run.status else {
        let secs = timeout.as_secs();
        return Err(StepFailure::retry(format!(
            "{name} timed out after {secs}s"
        )));
    };
    let result = match tool {
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
    };
    let outcome = if result.is_ok() { "ok" } else { "failed" };
    eprintln!("{name} {outcome} after {}s", started.elapsed().as_secs());
    result
}

/// Media type of an image the AI CLIs accept, from its leading bytes; None for anything else,
/// such as SVG or an HTML page saved under an image name. The extension is not trusted: the API
/// rejects an image whose bytes do not match the declared type.
pub(crate) fn media_type(head: &[u8]) -> Option<&'static str> {
    if head.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if head.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some("image/jpeg")
    } else if head.starts_with(b"GIF87a") || head.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if head.starts_with(b"RIFF") && head.get(8..12) == Some(b"WEBP") {
        Some("image/webp")
    } else {
        None
    }
}

/// The single stream-json user message: the prompt, then each image as a base64 block behind a
/// `[N]` text block, so the numbers the prompt uses sit next to their images.
fn claude_message(prompt: &str, images: &[PathBuf]) -> Result<String, StepFailure> {
    let mut content = vec![json!({"type": "text", "text": prompt})];
    for (i, path) in images.iter().enumerate() {
        let bytes = fs::read(path)?;
        let media_type = media_type(&bytes).ok_or_else(|| {
            StepFailure::permanent(format!("unsupported image {}", path.display()))
        })?;
        let data = BASE64.encode(bytes);
        content.push(json!({"type": "text", "text": format!("[{}]", i + 1)}));
        content.push(json!({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }));
    }
    let message = json!({"type": "user", "message": {"role": "user", "content": content}});
    Ok(format!("{message}\n"))
}

/// The fields of claude's stream-json events this adapter reads: the `result` event, and the
/// error field any event may carry.
#[derive(Deserialize)]
struct ClaudeEvent {
    #[serde(rename = "type")]
    kind: String,
    subtype: Option<String>,
    is_error: Option<bool>,
    result: Option<String>,
    error: Option<serde_json::Value>,
}

/// Success needs exit 0 and a `result` event with is_error=false, subtype=success and a
/// non-empty result. A failed run still ends with that event, and its result is what tells auth
/// from rate limit.
fn claude_result(status: ExitStatus, stdout: &str, stderr: &str) -> Result<String, StepFailure> {
    let result_event = stdout
        .lines()
        .rev()
        .filter_map(|line| serde_json::from_str::<ClaudeEvent>(line).ok())
        .find(|event| event.kind == "result");
    let detail = match result_event {
        Some(envelope) => {
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
        None => return Err(classify("claude", status, &claude_errors(stdout, stderr))),
    };
    Err(classify("claude", status, &format!("{detail} {stderr}")))
}

/// Failure text of a stream that never got to its result event: stderr first, so the 500
/// characters of the message keep it, then stdout lines that are not JSON and the error fields
/// of the events. Assistant text is never matched against the markers, and the long init event
/// is left out.
fn claude_errors(stdout: &str, stderr: &str) -> String {
    let mut parts = vec![stderr.trim().to_owned()];
    for line in stdout.lines().filter(|l| !l.trim().is_empty()) {
        match serde_json::from_str::<ClaudeEvent>(line) {
            Ok(event) => parts.extend(event.error.map(|error| match error {
                serde_json::Value::String(text) => text,
                other => other.to_string(),
            })),
            Err(_) => parts.push(line.to_owned()),
        }
    }
    parts.retain(|p| !p.is_empty());
    parts.join("; ")
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
