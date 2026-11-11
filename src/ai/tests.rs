//! Shared fixtures for the AI, translate and publish tests live here as `pub(crate)` helpers.

use std::{
    collections::BTreeMap, ffi::OsString, fs, os::unix::fs::PermissionsExt, path::Path,
    time::Instant,
};

use tempfile::TempDir;

use super::*;
use crate::pipeline::{
    self, AiSpec, JobSpec, Manifest, MetaValue, Pipeline, Platform, Source, StepExit, StepName,
    TranslateMode,
};

/// HOME for the fake CLIs (they log to `$HOME/fake-ai.log`) plus a job directory.
pub(crate) struct Fixture {
    pub home: TempDir,
    pub job: TempDir,
}

impl Fixture {
    pub(crate) fn new(pipeline: Pipeline, tool: AiTool) -> Fixture {
        let fixture = Fixture {
            home: tempfile::tempdir().unwrap(),
            job: tempfile::tempdir().unwrap(),
        };
        fixture.write_job(&job_spec(pipeline, tool));
        fixture
    }

    pub(crate) fn write_job(&self, job: &JobSpec) {
        fs::write(
            self.job.path().join(pipeline::JOB_FILE),
            serde_json::to_string(job).unwrap(),
        )
        .unwrap();
    }

    pub(crate) fn job_dir(&self) -> &Path {
        self.job.path()
    }

    /// Writes `steps/<nn>-<name>/` with the given files and a manifest. `files` maps logical
    /// names to (relative path, content).
    pub(crate) fn step(
        &self,
        seq: u32,
        name: StepName,
        title: Option<&str>,
        files: &[(&str, &str, &str)],
        assets: &[&str],
        meta: &[(&str, MetaValue)],
    ) -> std::path::PathBuf {
        let dir = self
            .job
            .path()
            .join("steps")
            .join(pipeline::step_dir_name(seq, name));
        fs::create_dir_all(dir.join("assets")).unwrap();
        let mut manifest = Manifest {
            title: title.map(str::to_owned),
            ..Manifest::default()
        };
        for (logical, rel, content) in files {
            fs::write(dir.join(rel), content).unwrap();
            manifest.files.insert((*logical).into(), (*rel).into());
        }
        for asset in assets {
            fs::write(dir.join("assets").join(asset), format!("image {asset}")).unwrap();
            manifest.assets.push((*asset).into());
        }
        manifest.meta = meta
            .iter()
            .map(|(k, v)| ((*k).to_owned(), v.clone()))
            .collect::<BTreeMap<_, _>>();
        pipeline::write_manifest(&dir, &manifest).unwrap();
        dir
    }

    /// A fresh, empty out_dir like the executor creates.
    pub(crate) fn out_dir(&self, name: &str) -> std::path::PathBuf {
        let dir = self.job.path().join("steps").join(format!("{name}.tmp"));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// HOME pointing at the fixture and a PATH that can only reach the fake CLIs: tests/fake,
    /// then /usr/bin:/bin for python3. Never the user's PATH, where the real claude lives.
    pub(crate) fn env(&self) -> Vec<(OsString, OsString)> {
        let fake = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake");
        for tool in ["claude", "codex"] {
            let mode = fs::metadata(fake.join(tool)).map_or(0, |m| m.permissions().mode());
            assert!(mode & 0o111 != 0, "tests/fake/{tool} must be executable");
            for dir in ["/usr/bin", "/bin"] {
                assert!(
                    !Path::new(dir).join(tool).exists(),
                    "a real {tool} is in {dir}"
                );
            }
        }
        let mut path = OsString::from(fake);
        path.push(":/usr/bin:/bin");
        vec![
            ("PATH".into(), path),
            ("HOME".into(), self.home.path().into()),
            ("TMPDIR".into(), self.home.path().into()),
        ]
    }

    /// One JSON object per fake CLI call.
    pub(crate) fn calls(&self) -> Vec<serde_json::Value> {
        match fs::read_to_string(self.home.path().join("fake-ai.log")) {
            Ok(log) => log
                .lines()
                .map(|l| serde_json::from_str(l).unwrap())
                .collect(),
            Err(_) => Vec::new(),
        }
    }
}

pub(crate) fn job_spec(pipeline: Pipeline, tool: AiTool) -> JobSpec {
    let source = match pipeline {
        Pipeline::Video => Source {
            platform: Platform::Youtube,
            id: "dQw4w9WgXcQ".into(),
            url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ".into(),
        },
        Pipeline::Arxiv => Source {
            platform: Platform::Arxiv,
            id: "2401.04088".into(),
            url: "https://arxiv.org/abs/2401.04088".into(),
        },
    };
    JobSpec {
        job_id: 12,
        pipeline,
        source,
        domain: "ml".into(),
        collection: None,
        translate: TranslateMode::Auto,
        ai: AiSpec {
            tool,
            model_note: "model-note".into(),
            model_translate: "model-translate".into(),
            cli_version: "test".into(),
        },
        created: "2026-11-08T10:00:00+08:00".into(),
        public_url: "http://nas.lan:8780/".into(),
        vault_name: "flori".into(),
    }
}

const SECOND: Duration = Duration::from_secs(1);

/// An image sent as it is, showing itself.
fn image(path: &Path) -> Image {
    Image {
        file: path.to_owned(),
        shows: path.to_owned(),
    }
}

#[test]
fn test_path_reaches_only_the_fakes() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let env = fx.env();
    let path = env
        .iter()
        .find(|(k, _)| k == "PATH")
        .unwrap()
        .1
        .to_str()
        .unwrap();
    let fake = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake");
    assert_eq!(path, format!("{}:/usr/bin:/bin", fake.display()));
    for dir in path.split(':').skip(1) {
        for tool in ["claude", "codex"] {
            assert!(!Path::new(dir).join(tool).exists(), "{dir}/{tool}");
        }
    }
}

async fn ask(tool: AiTool, prompt: &str) -> (Fixture, Result<String, StepFailure>) {
    let fx = Fixture::new(Pipeline::Arxiv, tool);
    let result = complete(&fx.env(), tool, "m-1", prompt, &[], 20 * SECOND).await;
    (fx, result)
}

fn exit(result: Result<String, StepFailure>) -> StepExit {
    result.expect_err("call should fail").exit
}

#[tokio::test]
async fn claude_gets_the_fixed_arguments_and_stdin() {
    let (fx, result) = ask(AiTool::Claude, &format!("指令\n{INPUT_MARKER}\nhello")).await;
    assert_eq!(result.unwrap(), "hello");
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    let args: Vec<&str> = calls[0]["args"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap())
        .collect();
    let fixed = "-p --model m-1 --tools  --input-format stream-json --output-format stream-json \
                 --verbose --max-turns 1 --no-session-persistence --strict-mcp-config \
                 --setting-sources project";
    assert_eq!(args.join(" "), fixed);
}

#[tokio::test]
async fn claude_always_runs_in_one_empty_cwd_under_home() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let cwd = fx.home.path().join("ai-cwd");
    fs::create_dir_all(cwd.join(".claude")).unwrap();
    fs::write(cwd.join(".claude/settings.json"), "{}").unwrap();
    for _ in 0..2 {
        let prompt = format!("x\n{INPUT_MARKER}\nhi");
        complete(&fx.env(), AiTool::Claude, "m-1", &prompt, &[], 20 * SECOND)
            .await
            .unwrap();
    }
    let calls = fx.calls();
    assert_eq!(calls.len(), 2);
    for call in &calls {
        assert_eq!(call["cwd"], cwd.to_str().unwrap());
        assert_eq!(call["cwd_entries"], serde_json::json!([]));
    }
}

#[tokio::test]
async fn codex_reads_the_last_message_file() {
    let (fx, result) = ask(AiTool::Codex, &format!("x\n{INPUT_MARKER}\n你好")).await;
    assert_eq!(result.unwrap(), "你好");
    let call = &fx.calls()[0];
    assert_eq!(call["model"], "m-1");
    let args: Vec<&str> = call["args"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap())
        .collect();
    let after = |flag: &str| args[args.iter().position(|a| *a == flag).unwrap() + 1];
    let (cwd, last) = (after("-C"), after("--output-last-message"));
    let fixed = format!(
        "--ask-for-approval never exec --strict-config --ignore-user-config --ignore-rules \
         --skip-git-repo-check --sandbox read-only --ephemeral --color never --json --model m-1 \
         -c history.persistence=\"none\" -c web_search=\"disabled\" -C {cwd} \
         --output-last-message {last} -"
    );
    assert_eq!(args.join(" "), fixed);
    // The reply file sits beside the empty cwd, and both are removed after the call.
    assert_eq!(Path::new(last).parent(), Path::new(cwd).parent());
    assert!(!Path::new(cwd).exists());
}

#[tokio::test]
async fn failures_are_classified_for_both_tools() {
    for tool in [AiTool::Claude, AiTool::Codex] {
        // FAKE_AUTH and FAKE_RATE_LIMIT exit 1; the class still comes from the parsed output.
        assert_eq!(
            exit(ask(tool, "FAKE_AUTH").await.1),
            StepExit::Auth,
            "{tool:?}"
        );
        assert_eq!(
            exit(ask(tool, "FAKE_RATE_LIMIT").await.1),
            StepExit::Retry,
            "{tool:?}"
        );
        assert_eq!(
            exit(ask(tool, "FAKE_FAIL").await.1),
            StepExit::Permanent,
            "{tool:?}"
        );
        assert_eq!(
            exit(ask(tool, "FAKE_EMPTY").await.1),
            StepExit::Permanent,
            "{tool:?}"
        );
    }
    // codex's real wording when the refresh token is gone.
    let refresh = ask(AiTool::Codex, "FAKE_AUTH_REFRESH").await.1;
    assert_eq!(exit(refresh), StepExit::Auth);
}

#[test]
fn claude_envelope_is_read_even_when_exit_is_nonzero() {
    use std::os::unix::process::ExitStatusExt;
    let failed = ExitStatus::from_raw(1 << 8);
    let ok = ExitStatus::from_raw(0);
    let envelope = |result: &str, is_error: bool| {
        format!(
            r#"{{"type":"result","subtype":"success","is_error":{is_error},"result":"{result}"}}"#
        )
    };
    let auth = claude_result(failed, &envelope("OAuth token has expired", true), "");
    assert_eq!(auth.unwrap_err().exit, StepExit::Auth);
    let limit = claude_result(
        failed,
        &envelope("API Error: Request rejected (429)", true),
        "",
    );
    assert_eq!(limit.unwrap_err().exit, StepExit::Retry);
    // A spend limit is not a rate limit.
    let spend = claude_result(ok, &envelope("Spend limit reached", true), "");
    assert_eq!(spend.unwrap_err().exit, StepExit::Permanent);
    // A non-zero exit with a success-looking envelope is still a failure.
    assert!(claude_result(failed, &envelope("text", false), "").is_err());
    assert_eq!(
        claude_result(ok, &envelope("text", false), "").unwrap(),
        "text"
    );
    let garbage = claude_result(failed, "not json", "Not logged in");
    assert_eq!(garbage.unwrap_err().exit, StepExit::Auth);
    // The result event is found among the other stream-json events.
    let stream = format!(
        "{{\"type\":\"system\",\"subtype\":\"init\"}}\n\
         {{\"type\":\"assistant\",\"message\":{{}}}}\n{}\n",
        envelope("streamed", false)
    );
    assert_eq!(claude_result(ok, &stream, "").unwrap(), "streamed");
}

#[tokio::test]
async fn a_stream_without_a_result_event_is_classified_from_stderr() {
    // A long init event, then "API Error: 429 rate limit exceeded" on stderr: the message keeps
    // the stderr text instead of 500 characters of init event.
    let (fx, result) = ask(AiTool::Claude, "FAKE_NO_RESULT_429").await;
    let failure = result.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.contains("API Error: 429"),
        "{}",
        failure.message
    );
    assert!(
        !failure.message.contains("slash_commands"),
        "{}",
        failure.message
    );
    assert_eq!(fx.calls().len(), 1);

    // Assistant text that mentions a rate limit is not a rate limit.
    let (_, result) = ask(AiTool::Claude, "FAKE_NO_RESULT_CHATTY").await;
    let failure = result.unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent, "{}", failure.message);
    assert!(
        failure.message.contains("connection reset"),
        "{}",
        failure.message
    );
}

#[test]
fn error_fields_of_stream_events_are_classified() {
    use std::os::unix::process::ExitStatusExt;
    let failed = ExitStatus::from_raw(1 << 8);
    let stdout =
        "{\"type\":\"assistant\",\"message\":{},\"error\":\"rate_limit\"}\nnot json at all\n";
    let failure = claude_result(failed, stdout, "").unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.contains("not json at all"),
        "{}",
        failure.message
    );
}

/// A minimal PNG or JPEG header with the given size; enough for the adapters and the fakes.
pub(crate) fn image_bytes(kind: &str, width: u16, height: u16) -> Vec<u8> {
    match kind {
        "png" => {
            let mut bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR".to_vec();
            bytes.extend(u32::from(width).to_be_bytes());
            bytes.extend(u32::from(height).to_be_bytes());
            bytes.extend([8, 6, 0, 0, 0]);
            bytes
        }
        _ => {
            // SOI, an APP0 segment to step over, then a baseline start-of-frame.
            let mut bytes = vec![0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x04, 0x4A, 0x46];
            bytes.extend([0xFF, 0xC0, 0x00, 0x11, 0x08]);
            bytes.extend(height.to_be_bytes());
            bytes.extend(width.to_be_bytes());
            bytes.extend([3; 10]);
            bytes
        }
    }
}

#[tokio::test]
async fn images_go_with_the_prompt_to_both_tools() {
    let dir = tempfile::tempdir().unwrap();
    let png = dir.path().join("fig.png");
    let jpg = dir.path().join("frame-1000.JPG");
    fs::write(&png, image_bytes("png", 640, 480)).unwrap();
    fs::write(&jpg, image_bytes("jpg", 1280, 720)).unwrap();
    let images = [image(&png), image(&jpg)];
    let prompt = format!("x\n{INPUT_MARKER}\nhi");

    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let env = fx.env();
    let reply = complete(&env, AiTool::Claude, "m-1", &prompt, &images, 20 * SECOND).await;
    // The fake joins the text blocks, so the [N] labels behind the prompt come back too.
    assert_eq!(reply.unwrap(), "hi\n[1]\n[2]");
    let sent = &fx.calls()[0]["images"];
    assert_eq!(sent[0]["media_type"], "image/png");
    assert!(
        sent[0]["head"]
            .as_str()
            .unwrap()
            .starts_with("89504e470d0a1a0a")
    );
    assert_eq!(sent[1]["media_type"], "image/jpeg");
    assert_eq!(sent[1]["bytes"], image_bytes("jpg", 1280, 720).len());
    // One user message: the prompt, then each image right behind its [N].
    assert_eq!(
        fx.calls()[0]["blocks"],
        serde_json::json!(["text", "[1]", "image", "[2]", "image"])
    );

    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Codex);
    let env = fx.env();
    let reply = complete(&env, AiTool::Codex, "m-1", &prompt, &images, 20 * SECOND).await;
    assert_eq!(reply.unwrap(), "hi");
    let call = &fx.calls()[0];
    let args: Vec<&str> = call["args"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap())
        .collect();
    // Each --image is followed by another flag, so the final `-` stays the prompt.
    let png_arg = format!(
        "--image {} --image {} --model",
        png.display(),
        jpg.display()
    );
    assert!(args.join(" ").contains(&png_arg), "{args:?}");
    assert_eq!(args.last(), Some(&"-"));
    assert_eq!(call["images"].as_array().unwrap().len(), 2);
}

#[tokio::test]
async fn an_unsupported_image_is_refused_before_spawning() {
    let dir = tempfile::tempdir().unwrap();
    let svg = dir.path().join("fig.svg");
    fs::write(&svg, "<svg/>").unwrap();
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let result = complete(&fx.env(), AiTool::Claude, "m", "hi", &[image(&svg)], SECOND).await;
    assert_eq!(exit(result), StepExit::Permanent);
    assert!(fx.calls().is_empty());
}

#[test]
fn codex_refresh_failures_and_login_prompts_are_auth() {
    use std::os::unix::process::ExitStatusExt;
    let failed = ExitStatus::from_raw(1 << 8);
    let texts = [
        "Your access token could not be refreshed because your refresh token has expired. \
         Please log out and sign in again.",
        "Your access token could not be refreshed because your refresh token was already used. \
         Please log out and sign in again.",
        "Your access token could not be refreshed because your refresh token was revoked. \
         Please sign in again.",
        "Invalid API key · Please run /login",
    ];
    for text in texts {
        assert_eq!(
            classify("codex", failed, text).exit,
            StepExit::Auth,
            "{text}"
        );
    }
}

#[tokio::test]
async fn timeout_is_retryable_and_quick() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let started = Instant::now();
    let result = complete(&fx.env(), AiTool::Claude, "m", "FAKE_HANG", &[], SECOND).await;
    assert_eq!(exit(result), StepExit::Retry);
    assert!(started.elapsed() < 8 * SECOND, "{:?}", started.elapsed());
}

#[tokio::test]
async fn missing_model_is_refused_before_spawning() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let result = complete(&fx.env(), AiTool::Claude, " ", "hi", &[], SECOND).await;
    assert_eq!(exit(result), StepExit::Permanent);
    assert!(fx.calls().is_empty());
}

fn alive(pid: &str) -> bool {
    // A zombie waiting for init to reap it counts as dead.
    fs::read_to_string(format!("/proc/{pid}/stat")).is_ok_and(|stat| {
        stat.rsplit(')')
            .next()
            .is_some_and(|s| !s.trim_start().starts_with('Z'))
    })
}

async fn wait_dead(pid: &str) -> bool {
    for _ in 0..100 {
        if !alive(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    false
}

/// A group that ignores SIGTERM is killed after the grace period, grandchildren included.
#[tokio::test]
async fn timeout_kills_the_whole_group_after_grace() {
    let dir = tempfile::tempdir().unwrap();
    let pid_file = dir.path().join("pid");
    let mut cmd = Command::new("sh");
    cmd.arg("-c")
        .arg(r#"trap "" TERM; sleep 600 & echo $! > "$0"; wait"#)
        .arg(&pid_file);
    let started = Instant::now();
    let run = run_cli(
        cmd,
        "",
        Duration::from_millis(500),
        Duration::from_millis(500),
    )
    .await
    .unwrap();
    assert!(run.status.is_none());
    assert!(started.elapsed() >= SECOND);
    let pid = fs::read_to_string(&pid_file).unwrap();
    assert!(wait_dead(pid.trim()).await, "grandchild {pid} survived");
}

/// SIGTERM to the step drops the running call, which kills the CLI's group at once.
#[tokio::test]
async fn sigterm_to_the_step_kills_the_cli() {
    let dir = tempfile::tempdir().unwrap();
    let pid_file = dir.path().join("pid");
    let mut cmd = Command::new("sh");
    cmd.arg("-c")
        .arg(r#"sleep 600 & echo $! > "$0"; wait"#)
        .arg(&pid_file);
    let pid_path = pid_file.clone();
    let step = async move {
        run_cli(cmd, "", 600 * SECOND, SECOND).await.unwrap();
        Ok(())
    };
    let signal = async move {
        while !pid_path.exists() {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        // The handler is installed before the step is first polled, so this cannot kill us.
        rustix::process::kill_process(rustix::process::getpid(), Signal::TERM).unwrap();
        std::future::pending::<()>().await;
    };
    let result = tokio::select! {
        result = until_terminated(step) => result,
        _ = signal => unreachable!(),
    };
    assert_eq!(result.unwrap_err().exit, StepExit::Retry);
    let pid = fs::read_to_string(&pid_file).unwrap();
    assert!(wait_dead(pid.trim()).await, "CLI child {pid} survived");
}

#[tokio::test]
async fn the_media_type_follows_the_bytes() {
    let dir = tempfile::tempdir().unwrap();
    let mislabeled = dir.path().join("photo.png");
    fs::write(&mislabeled, image_bytes("jpg", 64, 64)).unwrap();
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let env = fx.env();
    let images = [image(&mislabeled)];
    complete(&env, AiTool::Claude, "m", "hi", &images, 20 * SECOND)
        .await
        .unwrap();
    assert_eq!(fx.calls()[0]["images"][0]["media_type"], "image/jpeg");
}

/// Runs tests/fake/claude on raw stdin and returns its exit code.
fn fake_claude_exit(fx: &Fixture, stdin: &str) -> Option<i32> {
    use std::{io::Write, process::Stdio};
    let mut child =
        std::process::Command::new(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake/claude"))
            .args(["-p", "--model", "m", "--input-format", "stream-json"])
            .args(["--output-format", "stream-json", "--verbose"])
            .env_clear()
            .envs(fx.env())
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    child.wait().unwrap().code()
}

/// The fake guards the message shape, so a change to claude_message that breaks it fails here.
#[test]
fn the_fake_claude_rejects_malformed_messages() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let text = |t: &str| serde_json::json!({"type": "text", "text": t});
    let image = serde_json::json!({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}});
    let message = |kind: &str, content: Vec<serde_json::Value>| {
        serde_json::json!({"type": kind, "message": {"role": "user", "content": content}})
            .to_string()
    };
    let good = message("user", vec![text("hi"), text("[1]"), image.clone()]);
    assert_eq!(fake_claude_exit(&fx, &format!("{good}\n")), Some(0));
    let bad = [
        // Two events instead of one message.
        format!("{good}\n{good}\n"),
        // The label after its image.
        message("user", vec![text("hi"), image.clone(), text("[1]")]),
        // Labels that do not count from 1.
        message("user", vec![text("hi"), text("[2]"), image.clone()]),
        // No label at all, or no prompt first.
        message("user", vec![text("hi"), image.clone()]),
        message("user", vec![image.clone()]),
        message("assistant", vec![text("hi")]),
    ];
    for stdin in bad {
        assert_eq!(fake_claude_exit(&fx, &stdin), Some(2), "{stdin}");
    }
}
