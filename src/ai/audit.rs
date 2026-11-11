//! The record of each AI CLI call in FLORI_AI_LOG_DIR (contract 1): the prompt as sent, how the
//! call went, the CLI's raw stdout whether or not a step can use the reply, and the end of its
//! stderr. It is only read afterwards and never decides a step: a failed write costs one log
//! line. Secrets travel in the environment, which the record leaves out.

use std::{
    fs,
    io::{self, Write},
    os::unix::process::ExitStatusExt,
    path::{Path, PathBuf},
    time::Instant,
};

use serde_json::{Value, json};

use super::{Env, Image, Run};
use crate::{
    db,
    pipeline::{self, AiTool, StepExit, StepFailure},
};

/// How much of the end of stderr the record keeps.
const STDERR_TAIL: usize = 16 * 1024;
/// More calls than any attempt makes; the bound only stops a runaway search for a free name.
const MAX_CALLS: u32 = 10_000;

/// One call's files, `<step>-a<attempt>-<k>.*`, from the moment its name is claimed.
pub(super) struct CallLog {
    dir: PathBuf,
    stem: String,
    call: Value,
    started: Instant,
}

impl CallLog {
    /// Claims the next free `<step>-a<attempt>-<k>` and writes the prompt and what is known before
    /// the call. None without FLORI_AI_LOG_DIR or when the directory cannot be written.
    pub(super) fn start(
        env: &Env,
        tool: AiTool,
        model: &str,
        argv: Vec<String>,
        prompt: &str,
        images: &[Image],
    ) -> Option<CallLog> {
        let var = |name: &str| {
            env.iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v.to_string_lossy().into_owned())
        };
        let dir = PathBuf::from(var("FLORI_AI_LOG_DIR")?);
        // Both come from the executor; anything else would not make a plain file name.
        let plain =
            |s: &String| !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
        let step = var("FLORI_STEP")
            .filter(plain)
            .unwrap_or_else(|| "ai".into());
        let attempt = var("FLORI_ATTEMPT")
            .filter(plain)
            .unwrap_or_else(|| "0".into());
        // ai/ sits in the job directory, beside job.json.
        let job_dir = dir.parent().unwrap_or(&dir).to_owned();
        let cli_version = pipeline::read_job(&job_dir)
            .ok()
            .map(|job| job.ai.cli_version);
        let shown: Vec<String> = images
            .iter()
            .map(|i| relative(&job_dir, &i.shows))
            .collect();
        let call = json!({
            "tool": tool,
            "model": model,
            "cli_version": cli_version,
            "argv": argv,
            "images": shown,
            "started": db::now(),
        });
        let text = sent_text(tool, prompt, &shown);
        let claimed = claim(&dir, &format!("{step}-a{attempt}"), &call).and_then(|stem| {
            fs::write(dir.join(format!("{stem}.prompt.md")), text)?;
            Ok(stem)
        });
        match claimed {
            Ok(stem) => Some(CallLog {
                dir,
                stem,
                call,
                started: Instant::now(),
            }),
            Err(e) => {
                eprintln!("ai log: cannot write in {}: {e}", dir.display());
                None
            }
        }
    }

    /// Completes the call info and keeps stdout verbatim, `.out.json` when it has lines and every
    /// one is JSON, as both CLIs print, and `.out.txt` otherwise, with the stderr tail beside it.
    /// `run` is None when the CLI could not be started.
    pub(super) fn finish(mut self, run: Option<&Run>, result: &Result<String, StepFailure>) {
        let status = run.and_then(|r| r.status);
        let error = result.as_ref().err();
        let fields = json!({
            "finished": db::now(),
            "duration_ms": u64::try_from(self.started.elapsed().as_millis()).unwrap_or(u64::MAX),
            "timed_out": run.is_some_and(|r| r.status.is_none()),
            "exit_code": status.and_then(|s| s.code()),
            "signal": status.and_then(|s| s.signal()),
            "outcome": if error.is_some() { "failed" } else { "ok" },
            "error_class": error.map(|e| match e.exit {
                StepExit::Auth => "auth",
                StepExit::Retry => "retry",
                StepExit::Permanent => "permanent",
            }),
            "error": error.map(|e| e.message.as_str()),
        });
        if let (Some(call), Value::Object(fields)) = (self.call.as_object_mut(), fields) {
            call.extend(fields);
        }
        let (stdout, stderr) = run.map_or(("", ""), |r| (r.stdout.as_str(), r.stderr.as_str()));
        let mut lines = stdout.lines().filter(|l| !l.trim().is_empty()).peekable();
        let json = lines.peek().is_some()
            && lines.all(|l| serde_json::from_str::<serde::de::IgnoredAny>(l).is_ok());
        let out = if json { "out.json" } else { "out.txt" };
        let written = self
            .write(out, stdout)
            .and_then(|()| self.write("err.txt", tail(stderr, STDERR_TAIL)))
            .and_then(|()| self.write("call.json", &pretty(&self.call)));
        if let Err(e) = written {
            eprintln!("ai log: cannot finish {}: {e}", self.stem);
        }
    }

    /// Replaces `<stem>.<kind>` whole, so a reader never sees half of it.
    fn write(&self, kind: &str, text: &str) -> io::Result<()> {
        let path = self.dir.join(format!("{}.{kind}", self.stem));
        let tmp = self.dir.join(format!("{}.{kind}.tmp", self.stem));
        fs::write(&tmp, text)?;
        fs::rename(&tmp, path)
    }
}

/// The text blocks of the message as the CLI got them, each image block written as `<image
/// path>` where it went: claude gets `[N]` before each image, codex gets the images as files.
fn sent_text(tool: AiTool, prompt: &str, images: &[String]) -> String {
    let mut text = prompt.to_owned();
    for (i, path) in images.iter().enumerate() {
        match tool {
            AiTool::Claude => text.push_str(&format!("\n[{}]\n<image {path}>", i + 1)),
            AiTool::Codex => text.push_str(&format!("\n<image {path}>")),
        }
    }
    text
}

/// Creates `<prefix>-<k>.call.json` for the first k not taken. create_new makes the name this
/// call's even if another writer races for it.
fn claim(dir: &Path, prefix: &str, call: &Value) -> io::Result<String> {
    fs::create_dir_all(dir)?;
    for k in 1..=MAX_CALLS {
        let stem = format!("{prefix}-{k}");
        let path = dir.join(format!("{stem}.call.json"));
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
        {
            Ok(mut file) => {
                file.write_all(pretty(call).as_bytes())?;
                return Ok(stem);
            }
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e),
        }
    }
    Err(io::Error::other(format!(
        "{MAX_CALLS} calls named {prefix}"
    )))
}

fn pretty(value: &Value) -> String {
    serde_json::to_string_pretty(value).unwrap_or_default() + "\n"
}

/// The path relative to the job directory, as the files endpoint takes it.
fn relative(job_dir: &Path, path: &Path) -> String {
    let path = path.strip_prefix(job_dir).unwrap_or(path);
    path.to_string_lossy().into_owned()
}

/// The last `max` bytes of `text`, starting on a character boundary.
fn tail(text: &str, max: usize) -> &str {
    let mut start = text.len().saturating_sub(max);
    while !text.is_char_boundary(start) {
        start += 1;
    }
    &text[start..]
}

#[cfg(test)]
mod tests;
