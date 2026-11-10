//! Shared fixtures for the AI, translate and publish tests live here as `pub(crate)` helpers.

use std::{
    collections::BTreeMap, ffi::OsString, fs, os::unix::fs::PermissionsExt, path::Path,
    time::Instant,
};

use tempfile::TempDir;

use super::*;
use crate::pipeline::{AiSpec, JobSpec, MetaValue, Platform, Source, StepExit, TranslateMode};

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
    let result = complete(&fx.env(), tool, "m-1", prompt, 20 * SECOND).await;
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
    let fixed = "-p --model m-1 --tools  --output-format json --max-turns 1 \
                 --no-session-persistence --strict-mcp-config --setting-sources project";
    assert_eq!(args.join(" "), fixed);
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
    let result = complete(&fx.env(), AiTool::Claude, "m", "FAKE_HANG", SECOND).await;
    assert_eq!(exit(result), StepExit::Retry);
    assert!(started.elapsed() < 8 * SECOND, "{:?}", started.elapsed());
}

#[tokio::test]
async fn missing_model_is_refused_before_spawning() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let result = complete(&fx.env(), AiTool::Claude, " ", "hi", SECOND).await;
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
async fn note_reads_the_arxiv_original_and_writes_note_md() {
    for tool in [AiTool::Claude, AiTool::Codex] {
        let fx = Fixture::new(Pipeline::Arxiv, tool);
        fx.step(
            1,
            StepName::ArxivFetch,
            Some("Mixtral of Experts"),
            &[],
            &[],
            &[],
        );
        let original = "# Mixtral\n\nWe introduce Mixtral 8x7B.\n";
        fx.step(
            2,
            StepName::ArxivConvert,
            None,
            &[("original", "original.md", original)],
            &[],
            &[],
        );
        let out = fx.out_dir("03-note");
        note_with(fx.job_dir(), &out, &fx.env()).await.unwrap();
        let note = fs::read_to_string(out.join("note.md")).unwrap();
        assert_eq!(note, format!("标题:Mixtral of Experts\n\n{original}"));
        let manifest = pipeline::read_manifest(&out).unwrap();
        assert_eq!(manifest.title, None);
        assert_eq!(manifest.files["note"], "note.md");
        let calls = fx.calls();
        assert_eq!(calls.len(), 1);
        assert_eq!(calls[0]["model"], "model-note");
    }
}

#[tokio::test]
async fn note_feeds_video_sections_with_times() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    fx.step(1, StepName::VideoFetch, Some("Talk"), &[], &[], &[]);
    let sections = r#"[{"start_ms":0,"frames":["frame-0.jpg"],"text":"Hello there."},
        {"start_ms":30000,"frames":[],"text":"  "},
        {"start_ms":3690000,"frames":[],"text":"Bye."}]"#;
    let files = [
        ("sections", "sections.json", sections),
        ("transcript", "transcript.md", "x"),
    ];
    fx.step(
        3,
        StepName::VideoFrames,
        None,
        &files,
        &["frame-0.jpg"],
        &[],
    );
    let out = fx.out_dir("04-note");
    note_with(fx.job_dir(), &out, &fx.env()).await.unwrap();
    let note = fs::read_to_string(out.join("note.md")).unwrap();
    assert_eq!(note, "标题:Talk\n\n[00:00] Hello there.\n[1:01:30] Bye.\n");
}

#[tokio::test]
async fn note_failure_leaves_no_manifest() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(1, StepName::ArxivFetch, None, &[], &[], &[]);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", "FAKE_AUTH")],
        &[],
        &[],
    );
    let out = fx.out_dir("03-note");
    let failure = note_with(fx.job_dir(), &out, &fx.env()).await.unwrap_err();
    assert_eq!(failure.exit, StepExit::Auth);
    assert!(!out.join(pipeline::MANIFEST_FILE).exists());
}
