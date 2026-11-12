use std::{collections::BTreeSet, time::Instant as StdInstant};

use super::*;
use crate::db::job_dir;
use crate::{
    api::JobSummary,
    pipeline::{TranslateMode, parse_source},
};

/// Test configuration: built-in steps run tests/fake/step, Python steps tests/fake/steps.
pub(crate) fn config(root: &Path) -> Config {
    let fake = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake");
    Config {
        data_dir: root.join("data"),
        listen: "127.0.0.1:0".into(),
        web_user: "flori".into(),
        web_password: "test-only".into(),
        static_dir: root.join("web"),
        steps_dir: fake.join("steps"),
        self_exe: fake.join("step"),
        proxy: Some("http://proxy.invalid:3128".into()),
        public_url: "http://nas.lan:8780".into(),
        vault_name: "flori".into(),
        ai_tool: AiTool::Claude,
        ai_model_note: "model-note".into(),
        ai_model_translate: "model-translate".into(),
        bili_sessdata: None,
        whisper_model: root.join("models/whisper-base"),
        claude_oauth_token: None,
        codex_home: None,
    }
}

pub(crate) fn tools() -> Tools {
    Tools {
        cli_version: "1.0".into(),
        ytdlp_version: "2026.10.21".into(),
    }
}

/// Writes `<job_dir>/fake/<step>`; see tests/fake/step.
pub(crate) fn fake(config: &Config, id: i64, file: &str, content: &str) {
    let dir = job_dir(&config.data_dir, id).join("fake");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join(file), content).unwrap();
}

/// Polls until the job reaches `status`, or panics after 15s.
pub(crate) async fn wait_status(db: &SqlitePool, id: i64, status: JobStatus) -> JobSummary {
    let start = StdInstant::now();
    loop {
        let (job, _) = db::get_job(db, id).await.unwrap().unwrap();
        if job.status == status {
            return job;
        }
        assert!(
            start.elapsed() < Duration::from_secs(15),
            "job {id} stuck at {:?}",
            job.status
        );
        sleep(Duration::from_millis(20)).await;
    }
}

pub(crate) async fn wait_for(what: &str, mut ready: impl FnMut() -> bool) {
    let start = StdInstant::now();
    while !ready() {
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "timed out waiting for {what}"
        );
        sleep(Duration::from_millis(20)).await;
    }
}

/// Whether the process is gone (a zombie counts as gone).
pub(crate) fn gone(pid: i32) -> bool {
    match fs::read_to_string(format!("/proc/{pid}/stat")) {
        Err(_) => true,
        Ok(stat) => stat
            .rsplit_once(") ")
            .is_some_and(|(_, rest)| rest.starts_with('Z')),
    }
}

struct World {
    _dir: tempfile::TempDir,
    db: SqlitePool,
    config: Arc<Config>,
    domain_id: i64,
}

async fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let db = db::open(&dir.path().join("t.sqlite")).await.unwrap();
    let config = Arc::new(config(dir.path()));
    let domain_id = db::create_domain(&db, "ml").await.unwrap().unwrap().id;
    World {
        _dir: dir,
        db,
        config,
        domain_id,
    }
}

impl World {
    async fn job(&self, link: &str, translate: TranslateMode) -> i64 {
        let source = parse_source(link).unwrap();
        let created = db::create_jobs(
            &self.db,
            &self.config,
            "1.0",
            self.domain_id,
            None,
            translate,
            &[source],
        )
        .await
        .unwrap()
        .unwrap();
        created[0].clone().unwrap().id
    }

    async fn start(&self) -> ExecHandle {
        launch(self.db.clone(), self.config.clone(), tools())
            .await
            .unwrap()
    }

    fn log(&self, id: i64) -> String {
        fs::read_to_string(job_dir(&self.config.data_dir, id).join("job.log")).unwrap_or_default()
    }

    async fn step(&self, id: i64, seq: usize) -> crate::api::StepInfo {
        db::steps(&self.db, id).await.unwrap().swap_remove(seq - 1)
    }
}

const YT: &str = "https://youtu.be/dQw4w9WgXcQ";
const BV: &str = "https://www.bilibili.com/video/BV1GJ411x7h7";

#[tokio::test]
async fn exit_10_is_retried_and_the_log_only_grows() {
    let w = world().await;
    let id = w.job(YT, TranslateMode::Off).await;
    fake(&w.config, id, "video-fetch", "10 0");
    let _exec = w.start().await;
    wait_status(&w.db, id, JobStatus::Succeeded).await;

    let fetch = w.step(id, 1).await;
    assert_eq!(
        (fetch.status, fetch.attempts, fetch.exit_code),
        (StepStatus::Succeeded, 2, Some(0))
    );
    assert_eq!(fetch.error, None);
    let log = w.log(id);
    let first = log.find("== 01-video-fetch attempt 1 ==").unwrap();
    let second = log.find("== 01-video-fetch attempt 2 ==").unwrap();
    assert!(
        first < second && log.contains("fake video-fetch attempt 1 token 10"),
        "{log}"
    );
    assert!(log.contains("== 05-publish attempt 1 =="), "{log}");
    let steps = job_dir(&w.config.data_dir, id).join("steps");
    let mut dirs: Vec<String> = fs::read_dir(&steps)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    dirs.sort();
    assert_eq!(
        dirs,
        [
            "01-video-fetch",
            "02-video-transcribe",
            "03-video-frames",
            "04-note",
            "05-publish"
        ]
    );
}

#[tokio::test]
async fn permanent_exits_are_not_retried_and_retryable_ones_run_out() {
    let w = world().await;
    let permanent = w.job(YT, TranslateMode::Auto).await;
    let auth = w.job(BV, TranslateMode::Auto).await;
    let no_manifest = w.job("2401.12345", TranslateMode::Off).await;
    let exhausted = w
        .job("https://youtu.be/aaaaaaaaaaa", TranslateMode::Off)
        .await;
    // All on steps with more than one attempt, so a retry would show in `attempts`.
    fake(&w.config, permanent, "video-transcribe", "20 0");
    fake(&w.config, auth, "video-fetch", "30 0");
    fake(&w.config, no_manifest, "publish", "nomanifest 0");
    fake(&w.config, exhausted, "video-fetch", "10");
    let _exec = w.start().await;

    let job = wait_status(&w.db, permanent, JobStatus::Failed).await;
    assert_eq!(job.current_step, Some(StepName::VideoTranscribe));
    let step = w.step(permanent, 2).await;
    assert_eq!(
        (step.status, step.attempts, step.exit_code),
        (StepStatus::Failed, 1, Some(20))
    );
    assert_eq!(step.error.as_deref(), Some("fake video-transcribe stderr"));
    assert_eq!(w.step(permanent, 3).await.status, StepStatus::Pending);
    assert!(
        !job_dir(&w.config.data_dir, permanent)
            .join("steps/02-video-transcribe.tmp")
            .exists()
    );

    wait_status(&w.db, auth, JobStatus::Failed).await;
    let step = w.step(auth, 1).await;
    assert_eq!(
        (step.status, step.attempts, step.exit_code),
        (StepStatus::Failed, 1, Some(30))
    );

    wait_status(&w.db, no_manifest, JobStatus::Failed).await;
    let step = w.step(no_manifest, 4).await;
    assert_eq!(
        (step.name, step.attempts, step.exit_code),
        (StepName::Publish, 1, Some(20))
    );
    assert!(step.error.unwrap().contains("manifest.json"));

    wait_status(&w.db, exhausted, JobStatus::Failed).await;
    let step = w.step(exhausted, 1).await;
    assert_eq!(
        (step.status, step.attempts, step.exit_code),
        (StepStatus::Failed, 3, Some(10))
    );
    assert_eq!(
        w.log(exhausted)
            .matches("== 01-video-fetch attempt")
            .count(),
        3
    );
}

#[tokio::test]
async fn timeouts_record_no_exit_code_and_retry_continues() {
    let w = world().await;
    let id = w.job("2401.12345", TranslateMode::Off).await;
    fake(&w.config, id, "publish", "hang");
    let exec = w.start().await;
    wait_status(&w.db, id, JobStatus::Failed).await;
    let step = w.step(id, 4).await;
    assert_eq!(
        (step.status, step.attempts, step.exit_code),
        (StepStatus::Failed, 2, None)
    );
    assert_eq!(step.error.as_deref(), Some("timed out after 2s"));

    fake(&w.config, id, "publish", "hang hang 0");
    db::retry_job(&w.db, id).await.unwrap().unwrap();
    exec.wake();
    wait_status(&w.db, id, JobStatus::Succeeded).await;
    assert_eq!(w.step(id, 4).await.attempts, 3);
}

#[tokio::test]
async fn steps_get_only_whitelisted_env_and_outputs_reach_the_job() {
    let w = world().await;
    let id = w.job(YT, TranslateMode::Auto).await;
    fake(
        &w.config,
        id,
        "video-fetch.json",
        r#"{"title":"First","files":{},"assets":[],"meta":{}}"#,
    );
    fake(
        &w.config,
        id,
        "note.json",
        r#"{"title":"Second","files":{},"assets":[],"meta":{}}"#,
    );
    let publish = r#"{"title":null,"files":{},"assets":[],"meta":{"vault_path":"flori/ml/unsorted/x/","note_file":"First.md"}}"#;
    fake(&w.config, id, "publish.json", publish);
    let _exec = w.start().await;
    wait_status(&w.db, id, JobStatus::Succeeded).await;
    let (job, vault_path) = db::get_job(&w.db, id).await.unwrap().unwrap();
    assert_eq!(job.title.as_deref(), Some("First"));
    assert_eq!(vault_path.as_deref(), Some("flori/ml/unsorted/x/"));

    let dir = job_dir(&w.config.data_dir, id);
    let env = |step: &str| -> BTreeSet<String> {
        let text = fs::read_to_string(dir.join(format!("fake/{step}.env"))).unwrap();
        text.lines()
            .filter_map(|l| l.split_once('=').map(|(k, _)| k.to_owned()))
            .filter(|k| !["PWD", "SHLVL", "_"].contains(&k.as_str()))
            .collect()
    };
    let base = [
        "FLORI_ATTEMPT",
        "FLORI_STEP",
        "HOME",
        "LANG",
        "PATH",
        "TMPDIR",
    ];
    let with = |extra: &[&str]| -> BTreeSet<String> {
        base.iter().chain(extra).map(|s| s.to_string()).collect()
    };
    assert_eq!(
        env("video-fetch"),
        with(&[
            "FLORI_CACHE_DIR",
            "FLORI_STEPS_DIR",
            "HTTPS_PROXY",
            "HTTP_PROXY"
        ])
    );
    assert_eq!(
        env("video-transcribe"),
        with(&["FLORI_STEPS_DIR", "FLORI_WHISPER_MODEL"])
    );
    assert_eq!(env("video-frames"), with(&[]));
    let ai = [
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "DISABLE_AUTOUPDATER",
        "DISABLE_UPDATES",
        "FLORI_AI_LOG_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
    ];
    assert_eq!(env("note"), with(&ai));
    assert_eq!(
        env("translate"),
        with(&[&ai[..], &["FLORI_CACHE_DIR"]].concat())
    );
    assert_eq!(env("publish"), with(&["FLORI_VAULT_DIR"]));
    // Each its own cache, outside the step directories.
    for step in ["video-fetch", "translate"] {
        let env = fs::read_to_string(dir.join(format!("fake/{step}.env"))).unwrap();
        let cache = dir.join("cache").join(step);
        let line = format!("FLORI_CACHE_DIR={}\n", cache.display());
        assert!(env.contains(&line), "{step}: {env}");
        assert!(cache.is_dir(), "{step}");
    }
    let translate = fs::read_to_string(dir.join("fake/translate.env")).unwrap();
    assert!(translate.contains("LANG=C.UTF-8"));
    for step in ["note", "translate"] {
        let env = fs::read_to_string(dir.join(format!("fake/{step}.env"))).unwrap();
        let ai_log = format!("FLORI_AI_LOG_DIR={}\n", dir.join("ai").display());
        assert!(env.contains(&ai_log), "{step}: {env}");
    }
    assert!(dir.join("ai").is_dir());
}

#[tokio::test]
async fn bilibili_fetch_gets_sessdata_and_no_proxy() {
    let dir = tempfile::tempdir().unwrap();
    let db = db::open(&dir.path().join("t.sqlite")).await.unwrap();
    let mut config = config(dir.path());
    config.bili_sessdata = Some("fake-sessdata".into());
    let w = World {
        domain_id: db::create_domain(&db, "ml").await.unwrap().unwrap().id,
        _dir: dir,
        db,
        config: Arc::new(config),
    };
    let id = w
        .job(
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            TranslateMode::Off,
        )
        .await;
    fake(&w.config, id, "video-transcribe", "20");
    let _exec = w.start().await;
    wait_status(&w.db, id, JobStatus::Failed).await;
    let env =
        fs::read_to_string(job_dir(&w.config.data_dir, id).join("fake/video-fetch.env")).unwrap();
    assert!(env.contains("FLORI_BILI_SESSDATA=fake-sessdata"));
    assert!(!env.contains("PROXY"));
}

#[tokio::test]
async fn restart_after_a_crash_requeues_and_cleans_tmp() {
    let w = world().await;
    let id = w.job(YT, TranslateMode::Off).await;
    // State a kill -9 leaves behind mid-step.
    db::claim_job(&w.db, id).await.unwrap();
    assert_eq!(db::start_attempt(&w.db, id, 1).await.unwrap(), Some(1));
    let dir = job_dir(&w.config.data_dir, id);
    fs::create_dir_all(dir.join("steps/01-video-fetch.tmp")).unwrap();
    fs::write(dir.join("steps/01-video-fetch.tmp/video.mp4.part"), "junk").unwrap();
    fs::write(
        dir.join("job.log"),
        "== 01-video-fetch attempt 1 ==\npartial line\n",
    )
    .unwrap();
    let other = w.job("2401.12345", TranslateMode::Off).await;
    fs::create_dir_all(job_dir(&w.config.data_dir, other).join("steps/01-arxiv-fetch.tmp"))
        .unwrap();

    let _exec = w.start().await;
    assert!(
        !job_dir(&w.config.data_dir, other)
            .join("steps/01-arxiv-fetch.tmp")
            .exists()
    );
    wait_status(&w.db, id, JobStatus::Succeeded).await;
    assert_eq!(w.step(id, 1).await.attempts, 2);
    let log = w.log(id);
    assert!(
        log.starts_with(
            "== 01-video-fetch attempt 1 ==\npartial line\n== 01-video-fetch attempt 2 =="
        ),
        "{log}"
    );
    assert!(!dir.join("steps/01-video-fetch/video.mp4.part").exists());
    wait_status(&w.db, other, JobStatus::Succeeded).await;
}

#[tokio::test]
async fn timeout_escalates_to_sigkill_for_the_whole_group() {
    let mut cmd = Command::new("bash");
    cmd.args(["-c", "trap '' TERM; sleep 30 & echo $!; wait"]);
    let mut pid = None;
    let started = StdInstant::now();
    let stop = run_child(cmd, Duration::from_millis(200), None, |stderr, line| {
        if !stderr {
            pid = String::from_utf8_lossy(line).parse::<i32>().ok();
        }
    })
    .await
    .unwrap();
    assert_eq!(stop, Stop::TimedOut);
    assert!(started.elapsed() >= Duration::from_millis(200) + KILL_GRACE);
    let pid = pid.unwrap();
    wait_for("grandchild exit", || gone(pid)).await;
}

#[tokio::test]
async fn exit_codes_and_lines_come_back() {
    let mut cmd = Command::new("bash");
    cmd.args(["-c", "echo out; printf 'no newline' >&2; exit 7"]);
    let mut lines = Vec::new();
    let stop = run_child(cmd, Duration::from_secs(5), None, |stderr, line| {
        lines.push((stderr, String::from_utf8_lossy(line).into_owned()));
    })
    .await
    .unwrap();
    assert_eq!(stop, Stop::Exited(Some(7)));
    lines.sort();
    assert_eq!(
        lines,
        [(false, "out".to_owned()), (true, "no newline".to_owned())]
    );
}

#[tokio::test]
async fn cli_version_is_probed_or_unknown() {
    let dir = tempfile::tempdir().unwrap();
    let cli = dir.path().join("cli");
    fs::write(&cli, "#!/bin/sh\necho '2.1.319 (Claude Code)'\n").unwrap();
    let mut perms = fs::metadata(&cli).unwrap().permissions();
    std::os::unix::fs::PermissionsExt::set_mode(&mut perms, 0o755);
    fs::set_permissions(&cli, perms).unwrap();
    assert_eq!(probe_version(Command::new(&cli)).await, "2.1.319");
    assert_eq!(
        probe_version(Command::new(dir.path().join("missing"))).await,
        "unknown"
    );
    assert_eq!(probe_version(Command::new("false")).await, "unknown");
}

fn env_names(path: PathBuf) -> BTreeSet<String> {
    let text = fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
    text.lines()
        .filter_map(|l| l.split_once('=').map(|(k, _)| k.to_owned()))
        .collect()
}

#[tokio::test]
async fn ai_credentials_reach_only_ai_steps() {
    let dir = tempfile::tempdir().unwrap();
    let db = db::open(&dir.path().join("t.sqlite")).await.unwrap();
    let mut codex = config(dir.path());
    codex.claude_oauth_token = Some("fake-token".into());
    codex.codex_home = Some(dir.path().join("codex"));
    codex.ai_tool = AiTool::Codex;
    let claude = Config {
        ai_tool: AiTool::Claude,
        ..codex.clone()
    };
    let domain = db::create_domain(&db, "ml").await.unwrap().unwrap().id;
    let mut ids = Vec::new();
    // job.json fixes the tool, so one executor runs a codex job and a claude job.
    for (config, link) in [(&codex, YT), (&claude, "2401.12345")] {
        let source = parse_source(link).unwrap();
        let created = db::create_jobs(
            &db,
            config,
            "1.0",
            domain,
            None,
            TranslateMode::Auto,
            &[source],
        )
        .await
        .unwrap()
        .unwrap();
        let id = created[0].clone().unwrap().id;
        fs::create_dir_all(job_dir(&config.data_dir, id).join("fake")).unwrap();
        ids.push(id);
    }
    let _exec = launch(db.clone(), Arc::new(codex.clone()), tools())
        .await
        .unwrap();
    for id in &ids {
        wait_status(&db, *id, JobStatus::Succeeded).await;
    }
    let env = |id: i64, step: &str| {
        env_names(job_dir(&codex.data_dir, id).join(format!("fake/{step}.env")))
    };
    let (video, paper) = (ids[0], ids[1]);
    for step in ["note", "translate"] {
        let names = env(video, step);
        assert!(
            names.contains("CLAUDE_CODE_OAUTH_TOKEN") && names.contains("CODEX_HOME"),
            "{step}"
        );
        let names = env(paper, step);
        assert!(
            names.contains("CLAUDE_CODE_OAUTH_TOKEN") && !names.contains("CODEX_HOME"),
            "claude {step}"
        );
    }
    for (id, step) in [
        (video, "video-fetch"),
        (video, "video-transcribe"),
        (video, "video-frames"),
        (video, "publish"),
        (paper, "arxiv-fetch"),
        (paper, "arxiv-convert"),
        (paper, "publish"),
    ] {
        let names = env(id, step);
        assert!(
            !names.contains("CLAUDE_CODE_OAUTH_TOKEN") && !names.contains("CODEX_HOME"),
            "{step}"
        );
    }
}

/// Hanging fake steps whose child is still alive, read from the pid files tests/fake/step
/// writes. Unlike the steps table this leaves out a step waiting between attempts.
fn live(config: &Config, jobs: &[i64]) -> Vec<(i64, String)> {
    let mut steps = Vec::new();
    for &id in jobs {
        let Ok(entries) = fs::read_dir(job_dir(&config.data_dir, id).join("fake")) else {
            continue;
        };
        for entry in entries {
            let path = entry.unwrap().path();
            let Some(step) = path
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .strip_suffix(".pid")
            else {
                continue;
            };
            let pid = fs::read_to_string(&path)
                .unwrap_or_default()
                .trim()
                .parse()
                .unwrap_or(0);
            if pid > 0 && !gone(pid) {
                steps.push((id, step.to_owned()));
            }
        }
    }
    steps
}

fn count(steps: &[(i64, String)], name: &str) -> usize {
    steps.iter().filter(|(_, step)| step == name).count()
}

/// Marks the steps before `name` succeeded, as a retry from that step would find them.
async fn skip_to(db: &SqlitePool, id: i64, name: StepName) {
    sqlx::query(
        "UPDATE steps SET status = 'succeeded' WHERE job_id = ?1 \
         AND seq < (SELECT seq FROM steps WHERE job_id = ?1 AND name = ?2)",
    )
    .bind(id)
    .bind(name)
    .execute(db)
    .await
    .unwrap();
}

/// Checks `limit` on the live steps until `settled` holds and for 300ms after.
async fn sample(
    w: &World,
    jobs: &[i64],
    settled: impl Fn(&[(i64, String)]) -> bool,
    limit: impl Fn(&[(i64, String)]) -> bool,
) {
    let start = StdInstant::now();
    let mut after = None;
    while after.is_none_or(|n| n < 30) {
        let steps = live(&w.config, jobs);
        assert!(limit(&steps), "limit broken: {steps:?}");
        after = after.map(|n| n + 1).or(settled(&steps).then_some(0));
        assert!(
            start.elapsed() < Duration::from_secs(10),
            "never settled: {steps:?}"
        );
        sleep(Duration::from_millis(10)).await;
    }
}

async fn cancel_all(w: &World, exec: &ExecHandle, ids: &[i64]) {
    for id in ids {
        let _ = db::cancel_job(&w.db, *id).await.unwrap();
        exec.cancel(*id);
    }
}

#[tokio::test]
async fn two_steps_run_at_once_and_the_queue_waits_beyond_the_window() {
    let w = world().await;
    let mut ids = Vec::new();
    for link in [
        YT,
        "https://youtu.be/aaaaaaaaaaa",
        "https://youtu.be/bbbbbbbbbbb",
        BV,
    ] {
        let id = w.job(link, TranslateMode::Off).await;
        fake(&w.config, id, "video-fetch", "hang");
        ids.push(id);
    }
    let exec = w.start().await;
    sample(&w, &ids, |s| s.len() == 2, |s| s.len() <= 2).await;
    let jobs = db::list_jobs(&w.db, 10).await.unwrap();
    let status = |status| jobs.iter().filter(|j| j.status == status).count();
    assert_eq!(
        (status(JobStatus::Running), status(JobStatus::Queued)),
        (JOB_WINDOW, 1)
    );
    assert_eq!(
        db::get_job(&w.db, ids[3]).await.unwrap().unwrap().0.status,
        JobStatus::Queued
    );
    cancel_all(&w, &exec, &ids).await;
}

#[tokio::test]
async fn ai_steps_run_one_at_a_time_and_waiting_holds_no_global_slot() {
    let w = world().await;
    let mut ids = Vec::new();
    for link in [YT, "2401.12345"] {
        let id = w.job(link, TranslateMode::Off).await;
        skip_to(&w.db, id, StepName::Note).await;
        fake(&w.config, id, "note", "hang");
        ids.push(id);
    }
    let exec = w.start().await;
    sample(
        &w,
        &ids,
        |s| count(s, "note") == 1,
        |s| count(s, "note") <= 1,
    )
    .await;
    // The second note now waits for the ai lock. A fetch arriving after it must still run,
    // which it cannot if that wait sits on a global slot.
    let fetcher = w.job(BV, TranslateMode::Off).await;
    fake(&w.config, fetcher, "video-fetch", "hang");
    ids.push(fetcher);
    exec.wake();
    sample(
        &w,
        &ids,
        |s| count(s, "video-fetch") == 1,
        |s| count(s, "note") <= 1,
    )
    .await;
    cancel_all(&w, &exec, &ids).await;
}

#[tokio::test]
async fn whisper_steps_run_one_at_a_time_and_apart_from_the_ai_lock() {
    let w = world().await;
    let mut ids = Vec::new();
    for link in [YT, BV] {
        let id = w.job(link, TranslateMode::Off).await;
        skip_to(&w.db, id, StepName::VideoTranscribe).await;
        fake(&w.config, id, "video-transcribe", "hang");
        ids.push(id);
    }
    let exec = w.start().await;
    let transcribes = |s: &[(i64, String)]| count(s, "video-transcribe");
    sample(&w, &ids, |s| transcribes(s) == 1, |s| transcribes(s) <= 1).await;
    // The second transcribe now waits for the whisper lock. A note arriving after it must run
    // beside the first, which it cannot if whisper and ai share a lock.
    let noter = w.job("2401.12345", TranslateMode::Off).await;
    skip_to(&w.db, noter, StepName::Note).await;
    fake(&w.config, noter, "note", "hang");
    ids.push(noter);
    exec.wake();
    let both = |s: &[(i64, String)]| count(s, "note") == 1 && transcribes(s) == 1;
    sample(&w, &ids, both, |s| transcribes(s) <= 1).await;
    cancel_all(&w, &exec, &ids).await;
}

#[tokio::test]
async fn local_publish_runs_while_both_global_slots_are_taken() {
    let w = world().await;
    let mut ids = Vec::new();
    for link in [YT, "https://youtu.be/aaaaaaaaaaa"] {
        let id = w.job(link, TranslateMode::Off).await;
        fake(&w.config, id, "video-fetch", "hang");
        ids.push(id);
    }
    let exec = w.start().await;
    sample(&w, &ids, |s| count(s, "video-fetch") == 2, |s| s.len() <= 2).await;
    let publisher = w.job("2401.12345", TranslateMode::Off).await;
    skip_to(&w.db, publisher, StepName::Publish).await;
    fake(&w.config, publisher, "publish", "hang");
    ids.push(publisher);
    exec.wake();
    let all = |s: &[(i64, String)]| count(s, "video-fetch") == 2 && count(s, "publish") == 1;
    sample(&w, &ids, all, |s| count(s, "video-fetch") <= 2).await;
    cancel_all(&w, &exec, &ids).await;
}

#[tokio::test]
async fn a_failed_step_left_by_a_crash_fails_the_job() {
    let w = world().await;
    let id = w.job(YT, TranslateMode::Off).await;
    db::claim_job(&w.db, id).await.unwrap();
    db::start_attempt(&w.db, id, 1).await.unwrap();
    let failed = StepStatus::Failed;
    db::end_attempt(&w.db, id, 1, failed, Some(20), Some("x"))
        .await
        .unwrap();
    // Killed here, before the job was marked failed.
    let _exec = w.start().await;
    wait_status(&w.db, id, JobStatus::Failed).await;
    assert_eq!(w.step(id, 1).await.attempts, 1);
}

#[tokio::test]
async fn database_write_failures_do_not_spin() {
    let w = world().await;
    let id = w.job(YT, TranslateMode::Off).await;
    // RAISE(FAIL) keeps the INSERT, so `claims` counts every attempt to claim the job.
    sqlx::raw_sql(
        "CREATE TABLE claims (n INTEGER); \
         CREATE TRIGGER disk_full BEFORE UPDATE ON jobs WHEN NEW.status = 'running' BEGIN \
         INSERT INTO claims VALUES (1); SELECT RAISE(FAIL, 'database or disk is full'); END;",
    )
    .execute(&w.db)
    .await
    .unwrap();
    let _exec = w.start().await;
    sleep(Duration::from_secs(1)).await;
    let claims: i64 = sqlx::query_scalar("SELECT count(*) FROM claims")
        .fetch_one(&w.db)
        .await
        .unwrap();
    assert!((1..10).contains(&claims), "{claims} claims in 1s");
    assert_eq!(
        db::get_job(&w.db, id).await.unwrap().unwrap().0.status,
        JobStatus::Queued
    );
}

#[test]
fn a_published_paper_lists_its_authors_and_a_bare_manifest_lists_nothing() {
    use crate::ai::tests::{Fixture, job_spec};
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let job = job_spec(Pipeline::Arxiv, AiTool::Claude);
    let mut publish = Manifest::default();
    assert_eq!(published(&job, fx.job_dir(), &publish), None, "no path");
    let path = MetaValue::Text("flori/ml/unsorted/x".into());
    publish.meta.insert("vault_path".into(), path);
    // The fetch step left nothing readable: the note still gets its path.
    let bare = published(&job, fx.job_dir(), &publish).unwrap();
    let want = Published {
        vault_path: "flori/ml/unsorted/x".into(),
        ..Published::default()
    };
    assert_eq!(bare, want);
    let authors = vec!["A. Author".to_owned(), "B. Author".to_owned()];
    let meta = [
        ("authors", MetaValue::List(authors)),
        ("published", MetaValue::Text("2024-01-08".into())),
        // Not a paper's field, and not a positive length either.
        ("duration_ms", MetaValue::Int(0)),
        ("uploader", MetaValue::Text("someone".into())),
    ];
    fx.step(1, StepName::ArxivFetch, Some("T"), &[], &[], &meta);
    let text = MetaValue::Text("  ".into());
    publish.meta.insert("description".into(), text);
    let listed = published(&job, fx.job_dir(), &publish).unwrap();
    let want = Published {
        vault_path: "flori/ml/unsorted/x".into(),
        author: Some("A. Author, B. Author".into()),
        published: Some("2024-01-08".into()),
        duration_ms: None,
        description: None,
    };
    assert_eq!(listed, want);
}
