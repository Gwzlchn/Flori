use std::{collections::BTreeSet, time::Instant as StdInstant};

use super::*;
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

pub(crate) fn job_dir(config: &Config, id: i64) -> PathBuf {
    config.data_dir.join("jobs").join(id.to_string())
}

/// Writes `<job_dir>/fake/<step>`; see tests/fake/step.
pub(crate) fn fake(config: &Config, id: i64, file: &str, content: &str) {
    let dir = job_dir(config, id).join("fake");
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
        launch(self.db.clone(), self.config.clone(), "1.0".into())
            .await
            .unwrap()
    }

    fn log(&self, id: i64) -> String {
        fs::read_to_string(job_dir(&self.config, id).join("job.log")).unwrap_or_default()
    }

    async fn step(&self, id: i64, seq: usize) -> crate::api::StepInfo {
        db::steps(&self.db, id).await.unwrap().swap_remove(seq - 1)
    }
}

const YT: &str = "https://youtu.be/dQw4w9WgXcQ";

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
    let steps = job_dir(&w.config, id).join("steps");
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
async fn exit_20_and_30_fail_without_retry() {
    let w = world().await;
    let permanent = w.job(YT, TranslateMode::Auto).await;
    let auth = w
        .job(
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            TranslateMode::Auto,
        )
        .await;
    let no_manifest = w.job("2401.12345", TranslateMode::Auto).await;
    fake(&w.config, permanent, "video-transcribe", "20 0");
    fake(&w.config, auth, "note", "30");
    fake(&w.config, no_manifest, "note", "nomanifest");
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
        !job_dir(&w.config, permanent)
            .join("steps/02-video-transcribe.tmp")
            .exists()
    );

    wait_status(&w.db, auth, JobStatus::Failed).await;
    let step = w.step(auth, 4).await;
    assert_eq!(
        (step.name, step.exit_code, step.attempts),
        (StepName::Note, Some(30), 1)
    );

    wait_status(&w.db, no_manifest, JobStatus::Failed).await;
    let step = w.step(no_manifest, 3).await;
    assert_eq!((step.name, step.exit_code), (StepName::Note, Some(20)));
    assert!(step.error.unwrap().contains("manifest.json"));
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

    let dir = job_dir(&w.config, id);
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
    assert_eq!(env("video-fetch"), with(&["HTTPS_PROXY", "HTTP_PROXY"]));
    assert_eq!(
        env("video-transcribe"),
        with(&["FLORI_STEPS_DIR", "FLORI_WHISPER_MODEL"])
    );
    assert_eq!(env("video-frames"), with(&[]));
    let ai = [
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "DISABLE_AUTOUPDATER",
        "DISABLE_UPDATES",
        "HTTPS_PROXY",
        "HTTP_PROXY",
    ];
    assert_eq!(env("note"), with(&ai));
    assert_eq!(
        env("translate"),
        with(&[&ai[..], &["FLORI_CACHE_DIR"]].concat())
    );
    assert_eq!(env("publish"), with(&["FLORI_VAULT_DIR"]));
    let translate = fs::read_to_string(dir.join("fake/translate.env")).unwrap();
    assert!(translate.contains(&format!(
        "FLORI_CACHE_DIR={}",
        dir.join("cache/translate").display()
    )));
    assert!(translate.contains("LANG=C.UTF-8"));
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
    let env = fs::read_to_string(job_dir(&w.config, id).join("fake/video-fetch.env")).unwrap();
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
    let dir = job_dir(&w.config, id);
    fs::create_dir_all(dir.join("steps/01-video-fetch.tmp")).unwrap();
    fs::write(dir.join("steps/01-video-fetch.tmp/video.mp4.part"), "junk").unwrap();
    fs::write(
        dir.join("job.log"),
        "== 01-video-fetch attempt 1 ==\npartial line\n",
    )
    .unwrap();
    let other = w.job("2401.12345", TranslateMode::Off).await;
    fs::create_dir_all(job_dir(&w.config, other).join("steps/01-arxiv-fetch.tmp")).unwrap();

    let _exec = w.start().await;
    assert!(
        !job_dir(&w.config, other)
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
