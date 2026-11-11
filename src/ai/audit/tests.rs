use std::{ffi::OsString, fs, path::Path};

use serde_json::Value;

use super::super::{Image, complete, tests::Fixture};
use crate::pipeline::{AiTool, Pipeline, StepExit};

const SECOND: std::time::Duration = std::time::Duration::from_secs(1);
const TOKEN: &str = "oauth-token-that-must-not-leak";
const PROXY_PASS: &str = "proxy-pass-that-must-not-leak";

/// The fixture's env as the executor gives an AI step: a log dir, the step, the attempt and
/// secrets that must stay out of the record.
fn step_env(fx: &Fixture, step: &str, attempt: u32) -> Vec<(OsString, OsString)> {
    let mut env = fx.env();
    env.extend([
        ("FLORI_AI_LOG_DIR".into(), fx.job_dir().join("ai").into()),
        ("FLORI_STEP".into(), step.into()),
        ("FLORI_ATTEMPT".into(), attempt.to_string().into()),
        ("CLAUDE_CODE_OAUTH_TOKEN".into(), TOKEN.into()),
        (
            "HTTPS_PROXY".into(),
            format!("http://flori:{PROXY_PASS}@127.0.0.1:9").into(),
        ),
    ]);
    env
}

/// File names in the job's ai/, sorted.
fn records(fx: &Fixture) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(fx.job_dir().join("ai"))
        .map(|dir| {
            dir.map(|e| e.unwrap().file_name().into_string().unwrap())
                .collect()
        })
        .unwrap_or_default();
    names.sort();
    names
}

fn read(fx: &Fixture, name: &str) -> String {
    fs::read_to_string(fx.job_dir().join("ai").join(name)).unwrap()
}

fn call_info(fx: &Fixture, stem: &str) -> Value {
    serde_json::from_str(&read(fx, &format!("{stem}.call.json"))).unwrap()
}

/// No file of the record holds a secret from the environment.
fn assert_no_secrets(fx: &Fixture) {
    for name in records(fx) {
        let text = read(fx, &name);
        assert!(!text.contains(TOKEN), "{name}");
        assert!(!text.contains(PROXY_PASS), "{name}");
    }
}

#[tokio::test]
async fn a_successful_call_keeps_its_prompt_info_and_raw_output() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    let assets = fx.job_dir().join("steps/03-video-frames/assets");
    fs::create_dir_all(&assets).unwrap();
    let frame = assets.join("frame-0.jpg");
    fs::write(&frame, super::super::tests::image_bytes("jpg", 64, 64)).unwrap();
    // The second image goes as a shrunk copy outside the job; the record names its asset.
    let elsewhere = tempfile::tempdir().unwrap();
    let copy = elsewhere.path().join("image-1.jpg");
    fs::write(&copy, super::super::tests::image_bytes("jpg", 32, 32)).unwrap();
    let images = [
        Image {
            file: frame.clone(),
            shows: frame,
        },
        Image {
            file: copy,
            shows: assets.join("frame-30000.jpg"),
        },
    ];
    let env = step_env(&fx, "note", 2);
    let prompt = "指令\n---FLORI-INPUT---\nhello";
    let reply = complete(&env, AiTool::Claude, "m-1", prompt, &images, 20 * SECOND).await;
    assert_eq!(reply.unwrap(), "hello\n[1]\n[2]");
    assert_eq!(
        records(&fx),
        [
            "note-a2-1.call.json",
            "note-a2-1.err.txt",
            "note-a2-1.out.json",
            "note-a2-1.prompt.md"
        ]
    );
    assert_eq!(
        read(&fx, "note-a2-1.prompt.md"),
        "指令\n---FLORI-INPUT---\nhello\n[1]\n<image steps/03-video-frames/assets/frame-0.jpg>\n\
         [2]\n<image steps/03-video-frames/assets/frame-30000.jpg>"
    );
    let call = call_info(&fx, "note-a2-1");
    assert_eq!(call["tool"], "claude");
    assert_eq!(call["model"], "m-1");
    assert_eq!(call["cli_version"], "test");
    let argv: Vec<&str> = call["argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap())
        .collect();
    assert_eq!(&argv[..4], ["claude", "-p", "--model", "m-1"]);
    assert_eq!(
        call["images"],
        serde_json::json!([
            "steps/03-video-frames/assets/frame-0.jpg",
            "steps/03-video-frames/assets/frame-30000.jpg"
        ])
    );
    for key in ["started", "finished"] {
        assert!(call[key].as_str().unwrap().ends_with("+08:00"), "{call}");
    }
    assert!(call["duration_ms"].is_u64());
    assert_eq!(call["exit_code"], 0);
    assert_eq!(call["outcome"], "ok");
    assert_eq!(call["timed_out"], false);
    assert_eq!(call["error_class"], Value::Null);
    // stdout verbatim: the fake's stream-json events, ending with the result.
    let out = read(&fx, "note-a2-1.out.json");
    let events: Vec<Value> = out
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(events.last().unwrap()["result"], "hello\n[1]\n[2]");
    assert_eq!(read(&fx, "note-a2-1.err.txt"), "");
    assert_no_secrets(&fx);

    // The next call of the same attempt takes the next number.
    complete(&env, AiTool::Claude, "m-1", "again", &[], 20 * SECOND)
        .await
        .unwrap();
    assert!(records(&fx).contains(&"note-a2-2.prompt.md".to_owned()));
    assert_eq!(read(&fx, "note-a2-2.prompt.md"), "again");
}

#[tokio::test]
async fn a_failed_call_keeps_its_output_and_class() {
    for (marker, class, exit) in [
        ("FAKE_AUTH", "auth", StepExit::Auth),
        ("FAKE_RATE_LIMIT", "retry", StepExit::Retry),
        ("FAKE_FAIL", "permanent", StepExit::Permanent),
    ] {
        let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
        let env = step_env(&fx, "translate", 1);
        let result = complete(&env, AiTool::Claude, "m", marker, &[], 20 * SECOND).await;
        assert_eq!(result.unwrap_err().exit, exit, "{marker}");
        let call = call_info(&fx, "translate-a1-1");
        assert_eq!(call["outcome"], "failed", "{marker}");
        assert_eq!(call["error_class"], class, "{marker}");
        assert_eq!(call["exit_code"], 1, "{marker}");
        assert!(call["error"].as_str().unwrap().contains("claude failed"));
        let out = read(&fx, "translate-a1-1.out.json");
        assert!(out.contains(r#""type": "result""#), "{out}");
        assert_no_secrets(&fx);
    }
}

#[tokio::test]
async fn a_cli_that_prints_no_json_is_kept_as_text_with_its_stderr() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let env = step_env(&fx, "note", 1);
    let result = complete(
        &env,
        AiTool::Claude,
        "m",
        "FAKE_NO_RESULT_429",
        &[],
        20 * SECOND,
    )
    .await;
    assert_eq!(result.unwrap_err().exit, StepExit::Retry);
    assert!(read(&fx, "note-a1-1.err.txt").contains("API Error: 429"));
    // The init event alone is JSON, so stdout is still .out.json.
    assert!(records(&fx).contains(&"note-a1-1.out.json".to_owned()));

    // Without any CLI on PATH nothing runs; the record says so and keeps an empty stdout.
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let mut env = step_env(&fx, "note", 1);
    for (key, value) in &mut env {
        if key == "PATH" {
            *value = "/nonexistent".into();
        }
    }
    let result = complete(&env, AiTool::Claude, "m", "hi", &[], 20 * SECOND).await;
    assert_eq!(result.unwrap_err().exit, StepExit::Permanent);
    let call = call_info(&fx, "note-a1-1");
    assert_eq!(
        (call["outcome"].as_str(), call["exit_code"].as_i64()),
        (Some("failed"), None)
    );
    assert_eq!(read(&fx, "note-a1-1.out.txt"), "");
}

#[tokio::test]
async fn an_unwritable_log_dir_never_fails_the_call() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let blocker = fx.job_dir().join("ai");
    fs::write(&blocker, "a file where the directory should be").unwrap();
    let env = step_env(&fx, "note", 1);
    let reply = complete(
        &env,
        AiTool::Claude,
        "m",
        "x\n---FLORI-INPUT---\nok",
        &[],
        20 * SECOND,
    )
    .await;
    assert_eq!(reply.unwrap(), "ok");
    assert!(blocker.is_file());
}

#[tokio::test]
async fn nothing_is_recorded_without_a_log_dir_or_before_the_cli_starts() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    complete(&fx.env(), AiTool::Claude, "m", "hi", &[], 20 * SECOND)
        .await
        .unwrap();
    assert!(!fx.job_dir().join("ai").exists());

    // A refused image or a missing model stop the call before the CLI starts.
    let svg = fx.job_dir().join("fig.svg");
    fs::write(&svg, "<svg/>").unwrap();
    let env = step_env(&fx, "note", 1);
    let image = Image {
        file: svg.clone(),
        shows: svg,
    };
    assert!(
        complete(&env, AiTool::Claude, "m", "hi", &[image], SECOND)
            .await
            .is_err()
    );
    assert!(
        complete(&env, AiTool::Claude, " ", "hi", &[], SECOND)
            .await
            .is_err()
    );
    assert!(records(&fx).is_empty());
    assert!(!Path::new(&fx.job_dir().join("ai")).exists());
}

#[tokio::test]
async fn a_step_or_attempt_that_is_no_plain_name_stays_inside_the_log_dir() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let mut env = step_env(&fx, "../escaped", 1);
    for (key, value) in &mut env {
        if key == "FLORI_ATTEMPT" {
            *value = "1/../../x".into();
        }
    }
    complete(&env, AiTool::Claude, "m", "hi", &[], 20 * SECOND)
        .await
        .unwrap();
    assert_eq!(
        records(&fx),
        [
            "ai-a0-1.call.json",
            "ai-a0-1.err.txt",
            "ai-a0-1.out.json",
            "ai-a0-1.prompt.md"
        ]
    );
    let mut beside: Vec<String> = fs::read_dir(fx.job_dir())
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    beside.sort();
    assert_eq!(beside, ["ai", "job.json"]);
}
