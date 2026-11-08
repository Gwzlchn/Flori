//! Executor: one tokio task per job with steps in order, global limits, child process groups with
//! timeouts, line-by-line job.log, env whitelist, cancel, and recovery on start.

use std::{
    collections::{HashMap, hash_map::Entry},
    ffi::OsString,
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{Arc, Mutex, MutexGuard, PoisonError},
    time::Duration,
};

use rustix::process::{Pid, Signal, WaitId, WaitIdOptions, kill_process_group, waitid};
use sqlx::SqlitePool;
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, BufReader},
    process::Command,
    sync::{Notify, Semaphore, SemaphorePermit, mpsc, watch},
    time::{Instant, sleep, sleep_until},
};

use crate::{
    Config,
    api::{JobStatus, StepStatus},
    db,
    pipeline::{
        AiTool, JobSpec, MetaValue, Platform, Resource, StepName, StepProgram, StepSpec, read_job,
        read_manifest, step_dir_name,
    },
};

#[cfg(not(test))]
const RETRY_DELAYS: [Duration; 2] = [Duration::from_secs(30), Duration::from_secs(120)];
#[cfg(test)]
const RETRY_DELAYS: [Duration; 2] = [Duration::from_millis(50), Duration::from_millis(100)];
/// Between SIGTERM and SIGKILL of a process group.
#[cfg(not(test))]
const KILL_GRACE: Duration = Duration::from_secs(10);
#[cfg(test)]
const KILL_GRACE: Duration = Duration::from_millis(300);
/// Fallback scan for queued jobs; `wake` normally triggers the scan.
const POLL: Duration = Duration::from_secs(5);
const PROBE_TIMEOUT: Duration = Duration::from_secs(10);

type Fallible<T> = Result<T, Box<dyn std::error::Error + Send + Sync>>;

/// Cheap handle the HTTP layer uses after it commits a state change.
#[derive(Clone)]
pub struct ExecHandle {
    inner: Arc<Inner>,
}

struct Inner {
    db: SqlitePool,
    config: Arc<Config>,
    /// Absolute paths: children run with their output directory as cwd.
    jobs_dir: PathBuf,
    self_exe: PathBuf,
    steps_dir: PathBuf,
    cli_version: String,
    wake: Notify,
    /// Jobs with a live task, and the cancel signal of each.
    running: Mutex<HashMap<i64, watch::Sender<bool>>>,
    global: Semaphore,
    whisper: Semaphore,
    ai: Semaphore,
}

impl ExecHandle {
    /// New or retried jobs are queued in the database; this only nudges the scheduler.
    pub fn wake(&self) {
        self.inner.wake.notify_one();
    }

    /// Kills the process group of the running step. The caller has already marked the job canceled.
    pub fn cancel(&self, job_id: i64) {
        if let Some(tx) = self.inner.running().get(&job_id) {
            tx.send_replace(true);
        }
    }

    /// `<tool> --version` probed at start, recorded in every new job.json.
    pub fn cli_version(&self) -> &str {
        &self.inner.cli_version
    }
}

impl Inner {
    fn running(&self) -> MutexGuard<'_, HashMap<i64, watch::Sender<bool>>> {
        self.running.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The resource lock comes first, so a step waiting for it holds no global slot.
    async fn permits(&self, resource: Resource) -> Vec<SemaphorePermit<'_>> {
        let own = match resource {
            Resource::Whisper => Some(&self.whisper),
            Resource::Ai => Some(&self.ai),
            Resource::Net | Resource::Cpu | Resource::Local => None,
        };
        let global = (resource != Resource::Local).then_some(&self.global);
        let mut permits = Vec::new();
        for semaphore in own.into_iter().chain(global) {
            permits.extend(semaphore.acquire().await.ok());
        }
        permits
    }

    fn command(
        &self,
        job: &JobSpec,
        job_dir: &Path,
        out: &Path,
        name: StepName,
        attempt: u32,
    ) -> io::Result<Command> {
        let mut cmd = match name.spec().program {
            StepProgram::BuiltIn => {
                let mut cmd = Command::new(&self.self_exe);
                cmd.args(["step", name.as_str()]);
                cmd
            }
            StepProgram::Python { script, subcommand } => {
                let mut cmd = Command::new("python3");
                cmd.arg("-I")
                    .arg(self.steps_dir.join(script))
                    .arg(subcommand);
                cmd
            }
        };
        cmd.arg(job_dir)
            .arg(out)
            .current_dir(out)
            .env_clear()
            .envs(base_env());
        let c = &*self.config;
        let mut env: Vec<(&str, OsString)> = vec![
            (
                "TMPDIR",
                std::env::var_os("TMPDIR").unwrap_or_else(|| "/tmp".into()),
            ),
            ("FLORI_STEP", name.as_str().into()),
            ("FLORI_ATTEMPT", attempt.to_string().into()),
        ];
        let proxy = c
            .proxy
            .iter()
            .flat_map(|p| [("HTTPS_PROXY", p.into()), ("HTTP_PROXY", p.into())]);
        match name {
            StepName::VideoFetch if job.source.platform == Platform::Youtube => env.extend(proxy),
            StepName::VideoFetch => {
                env.extend(
                    c.bili_sessdata
                        .iter()
                        .map(|s| ("FLORI_BILI_SESSDATA", s.into())),
                );
            }
            StepName::VideoTranscribe => env.extend([
                (
                    "FLORI_WHISPER_MODEL",
                    std::path::absolute(&c.whisper_model)?.into(),
                ),
                ("FLORI_STEPS_DIR", self.steps_dir.clone().into()),
            ]),
            StepName::Publish => {
                let vault = std::path::absolute(c.data_dir.join("vault"))?;
                env.push(("FLORI_VAULT_DIR", vault.into()));
            }
            StepName::Note | StepName::Translate => {
                env.extend(proxy);
                for flag in [
                    "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
                    "DISABLE_AUTOUPDATER",
                    "DISABLE_UPDATES",
                ] {
                    env.push((flag, "1".into()));
                }
                env.extend(
                    c.claude_oauth_token
                        .iter()
                        .map(|t| ("CLAUDE_CODE_OAUTH_TOKEN", t.into())),
                );
                if job.ai.tool == AiTool::Codex {
                    env.extend(c.codex_home.iter().map(|h| ("CODEX_HOME", h.into())));
                }
                if name == StepName::Translate {
                    let cache = job_dir.join("cache/translate");
                    fs::create_dir_all(&cache)?;
                    env.push(("FLORI_CACHE_DIR", cache.into()));
                }
            }
            StepName::VideoFrames | StepName::ArxivFetch | StepName::ArxivConvert => {}
        }
        cmd.envs(env);
        Ok(cmd)
    }
}

/// PATH, LANG and HOME for a child that starts from a cleared environment.
pub(crate) fn base_env() -> [(&'static str, OsString); 3] {
    [
        (
            "PATH",
            std::env::var_os("PATH").unwrap_or_else(|| "/usr/local/bin:/usr/bin:/bin".into()),
        ),
        ("LANG", "C.UTF-8".into()),
        (
            "HOME",
            std::env::var_os("HOME").unwrap_or_else(|| "/".into()),
        ),
    ]
}

pub async fn start(db: SqlitePool, config: Arc<Config>) -> Result<ExecHandle, String> {
    let tool = match config.ai_tool {
        AiTool::Claude => "claude",
        AiTool::Codex => "codex",
    };
    let cli_version = probe_version(Command::new(tool)).await;
    launch(db, config, cli_version).await
}

/// Recovers from a crash, then starts the scheduler.
pub(crate) async fn launch(
    db: SqlitePool,
    config: Arc<Config>,
    cli_version: String,
) -> Result<ExecHandle, String> {
    let abs = |p: &Path| std::path::absolute(p).map_err(|e| format!("{}: {e}", p.display()));
    let jobs_dir = abs(&config.data_dir.join("jobs"))?;
    fs::create_dir_all(&jobs_dir).map_err(|e| format!("{}: {e}", jobs_dir.display()))?;
    db::recover(&db)
        .await
        .map_err(|e| format!("recover jobs: {e}"))?;
    remove_tmp_dirs(&jobs_dir).map_err(|e| format!("remove .tmp directories: {e}"))?;
    let inner = Arc::new(Inner {
        db,
        jobs_dir,
        self_exe: abs(&config.self_exe)?,
        steps_dir: abs(&config.steps_dir)?,
        config,
        cli_version,
        wake: Notify::new(),
        running: Mutex::default(),
        global: Semaphore::new(2),
        whisper: Semaphore::new(1),
        ai: Semaphore::new(1),
    });
    tokio::spawn(schedule(inner.clone()));
    Ok(ExecHandle { inner })
}

fn remove_tmp_dirs(jobs_dir: &Path) -> io::Result<()> {
    for job in fs::read_dir(jobs_dir)? {
        let Ok(steps) = fs::read_dir(job?.path().join("steps")) else {
            continue;
        };
        for entry in steps {
            let path = entry?.path();
            if path.extension().is_some_and(|ext| ext == "tmp") {
                fs::remove_dir_all(&path)?;
            }
        }
    }
    Ok(())
}

async fn schedule(inner: Arc<Inner>) {
    loop {
        match db::queued_jobs(&inner.db).await {
            Ok(ids) => {
                for id in ids {
                    let (tx, rx) = watch::channel(false);
                    if let Entry::Vacant(slot) = inner.running().entry(id) {
                        slot.insert(tx);
                        tokio::spawn(run_job(inner.clone(), id, rx));
                    }
                }
            }
            Err(err) => eprintln!("flori: scan queued jobs: {err}"),
        }
        tokio::select! {
            () = inner.wake.notified() => {}
            () = sleep(POLL) => {}
        }
    }
}

async fn run_job(inner: Arc<Inner>, id: i64, mut cancel: watch::Receiver<bool>) {
    if let Err(err) = drive(&inner, id, &mut cancel).await {
        eprintln!("flori: job {id}: {err}");
        if let Err(err) = db::fail_job(&inner.db, id, &err.to_string()).await {
            eprintln!("flori: job {id}: mark failed: {err}");
        }
    }
    inner.running().remove(&id);
    // A retry committed while this task was still stopping is picked up now.
    inner.wake.notify_one();
}

async fn drive(inner: &Inner, id: i64, cancel: &mut watch::Receiver<bool>) -> Fallible<()> {
    if !db::claim_job(&inner.db, id).await? {
        return Ok(());
    }
    let job_dir = inner.jobs_dir.join(id.to_string());
    let job = read_job(&job_dir)?;
    for (seq, name, status) in db::job_steps(&inner.db, id).await? {
        if status == StepStatus::Succeeded {
            continue;
        }
        match run_step(inner, &job, &job_dir, seq, name.spec(), cancel).await? {
            End::Succeeded => {}
            End::Failed => return Ok(db::end_job(&inner.db, id, JobStatus::Failed).await?),
            End::Stopped => return Ok(()),
        }
    }
    Ok(db::end_job(&inner.db, id, JobStatus::Succeeded).await?)
}

enum End {
    Succeeded,
    Failed,
    /// Canceled, or the job left `running` under us.
    Stopped,
}

async fn run_step(
    inner: &Inner,
    job: &JobSpec,
    job_dir: &Path,
    seq: u32,
    spec: StepSpec,
    cancel: &mut watch::Receiver<bool>,
) -> Fallible<End> {
    let name = step_dir_name(seq, spec.name);
    let dir = job_dir.join("steps").join(&name);
    let tmp = job_dir.join("steps").join(format!("{name}.tmp"));
    // Output of an attempt whose result never reached the database.
    remove_dir(&dir)?;
    let job_id = job.job_id;
    for try_no in 0..spec.attempts as usize {
        let permits = tokio::select! {
            permits = inner.permits(spec.resource) => permits,
            () = canceled(cancel) => return Ok(End::Stopped),
        };
        let Some(attempt) = db::start_attempt(&inner.db, job_id, seq).await? else {
            return Ok(End::Stopped);
        };
        remove_dir(&tmp)?;
        fs::create_dir_all(&tmp)?;
        let mut log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(job_dir.join("job.log"))?;
        writeln!(log, "== {name} attempt {attempt} ==")?;
        let mut last_err = None;
        let cmd = inner.command(job, job_dir, &tmp, spec.name, attempt)?;
        let stop = run_child(cmd, spec.timeout, Some(cancel), |stderr, line| {
            let _ = log.write_all(line).and_then(|()| log.write_all(b"\n"));
            if stderr && !line.trim_ascii().is_empty() {
                last_err = Some(String::from_utf8_lossy(line).into_owned());
            }
        })
        .await?;
        drop(permits);
        let (code, error, retry) = match stop {
            Stop::Canceled => {
                remove_dir(&tmp)?;
                let status = StepStatus::Canceled;
                db::end_attempt(&inner.db, job_id, seq, status, None, Some("canceled")).await?;
                return Ok(End::Stopped);
            }
            Stop::TimedOut => (
                None,
                format!("timed out after {}s", spec.timeout.as_secs()),
                true,
            ),
            Stop::Exited(Some(0)) => match read_manifest(&tmp) {
                Ok(manifest) => {
                    fs::rename(&tmp, &dir)?;
                    let status = StepStatus::Succeeded;
                    if db::end_attempt(&inner.db, job_id, seq, status, Some(0), None).await? {
                        let vault_path = match manifest.meta.get("vault_path") {
                            Some(MetaValue::Text(path)) if spec.name == StepName::Publish => {
                                Some(path.as_str())
                            }
                            _ => None,
                        };
                        let title = manifest.title.as_deref();
                        db::set_job_output(&inner.db, job_id, title, vault_path).await?;
                    }
                    return Ok(End::Succeeded);
                }
                // Contract 1: exit 0 without a usable manifest counts as exit 20.
                Err(err) => (
                    Some(20),
                    format!("exit 0 without a valid manifest.json: {err}"),
                    false,
                ),
            },
            Stop::Exited(code) => {
                let error = last_err.unwrap_or_else(|| match code {
                    Some(code) => format!("exit code {code}"),
                    None => "killed by a signal".to_owned(),
                });
                (code, error, !matches!(code, Some(20 | 30)))
            }
        };
        remove_dir(&tmp)?;
        if !retry || try_no + 1 == spec.attempts as usize {
            let status = StepStatus::Failed;
            db::end_attempt(&inner.db, job_id, seq, status, code, Some(&error)).await?;
            return Ok(End::Failed);
        }
        let status = StepStatus::Running;
        db::end_attempt(&inner.db, job_id, seq, status, code, Some(&error)).await?;
        tokio::select! {
            () = sleep(RETRY_DELAYS[try_no.min(RETRY_DELAYS.len() - 1)]) => {}
            () = canceled(cancel) => return Ok(End::Stopped),
        }
    }
    Ok(End::Failed)
}

fn remove_dir(path: &Path) -> io::Result<()> {
    match fs::remove_dir_all(path) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}

async fn canceled(rx: &mut watch::Receiver<bool>) {
    while !*rx.borrow_and_update() {
        if rx.changed().await.is_err() {
            std::future::pending::<()>().await;
        }
    }
}

/// First line of `<tool> --version`, narrowed to its first token that starts with a digit.
async fn probe_version(mut cmd: Command) -> String {
    cmd.arg("--version").env_clear().envs(base_env());
    let mut first: Option<String> = None;
    let stop = run_child(cmd, PROBE_TIMEOUT, None, |stderr, line| {
        if !stderr && first.is_none() {
            first = Some(String::from_utf8_lossy(line).trim().to_owned());
        }
    })
    .await;
    match (stop, first) {
        (Ok(Stop::Exited(Some(0))), Some(line)) if !line.is_empty() => line
            .split_whitespace()
            .find(|t| t.starts_with(|c: char| c.is_ascii_digit()))
            .unwrap_or(&line)
            .to_owned(),
        _ => "unknown".to_owned(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Stop {
    Exited(Option<i32>),
    TimedOut,
    Canceled,
}

/// Runs `cmd` in its own process group and hands each output line, without its newline, to
/// `on_line(is_stderr, line)`. On timeout or cancel the group gets SIGTERM and, after
/// `KILL_GRACE`, SIGKILL. Whatever is left in the group when the child exits is killed too.
pub(crate) async fn run_child(
    mut cmd: Command,
    timeout: Duration,
    cancel: Option<&mut watch::Receiver<bool>>,
    mut on_line: impl FnMut(bool, &[u8]),
) -> io::Result<Stop> {
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .kill_on_drop(true);
    let mut child = cmd.spawn()?;
    let pid = child
        .id()
        .and_then(|id| Pid::from_raw(id as i32))
        .ok_or_else(|| io::Error::other("child has no pid"))?;
    let mut group = Group(Some(pid));
    let (tx, mut rx) = mpsc::unbounded_channel();
    let readers = [
        tokio::spawn(read_lines(child.stdout.take(), false, tx.clone())),
        tokio::spawn(read_lines(child.stderr.take(), true, tx)),
    ];
    let cancel = async move {
        match cancel {
            Some(rx) => canceled(rx).await,
            None => std::future::pending().await,
        }
    };
    let far = Instant::now() + Duration::from_secs(10 * 365 * 86_400);
    let (deadline, kill_at) = (sleep(timeout), sleep_until(far));
    tokio::pin!(cancel, deadline, kill_at);
    let mut poll = tokio::time::interval(Duration::from_millis(20));
    let (mut stop, mut killed) = (None, false);
    loop {
        let new_stop = tokio::select! {
            Some((stderr, line)) = rx.recv() => {
                on_line(stderr, &line);
                None
            }
            _ = poll.tick() => {
                if exited(pid) {
                    break;
                }
                None
            }
            () = &mut deadline, if stop.is_none() => Some(Stop::TimedOut),
            () = &mut cancel, if stop.is_none() => Some(Stop::Canceled),
            () = &mut kill_at, if stop.is_some() && !killed => {
                killed = true;
                let _ = kill_process_group(pid, Signal::KILL);
                None
            }
        };
        if let Some(new_stop) = new_stop {
            stop = Some(new_stop);
            let _ = kill_process_group(pid, Signal::TERM);
            kill_at.as_mut().reset(Instant::now() + KILL_GRACE);
        }
    }
    // The child is not reaped yet, so its pid still names the group.
    let _ = kill_process_group(pid, Signal::KILL);
    let status = child.wait().await?;
    group.0 = None;
    let drain = sleep(Duration::from_secs(2));
    tokio::pin!(drain);
    loop {
        tokio::select! {
            line = rx.recv() => match line {
                Some((stderr, line)) => on_line(stderr, &line),
                None => break,
            },
            () = &mut drain => break,
        }
    }
    for reader in readers {
        reader.abort();
    }
    Ok(stop.unwrap_or(Stop::Exited(status.code())))
}

/// Kills the group if `run_child` is dropped before it reaps the child.
struct Group(Option<Pid>);

impl Drop for Group {
    fn drop(&mut self) {
        if let Some(pid) = self.0 {
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }
}

/// Exit check that leaves the child unreaped (WNOWAIT).
fn exited(pid: Pid) -> bool {
    let options = WaitIdOptions::EXITED | WaitIdOptions::NOWAIT | WaitIdOptions::NOHANG;
    !matches!(waitid(WaitId::Pid(pid), options), Ok(None))
}

async fn read_lines(
    pipe: Option<impl AsyncRead + Unpin>,
    stderr: bool,
    tx: mpsc::UnboundedSender<(bool, Vec<u8>)>,
) {
    let Some(pipe) = pipe else {
        return;
    };
    let mut reader = BufReader::new(pipe);
    loop {
        let mut line = Vec::new();
        match reader.read_until(b'\n', &mut line).await {
            Ok(0) | Err(_) => return,
            Ok(_) => {
                if line.ends_with(b"\n") {
                    line.pop();
                }
                if line.ends_with(b"\r") {
                    line.pop();
                }
                if tx.send((stderr, line)).is_err() {
                    return;
                }
            }
        }
    }
}

#[cfg(test)]
pub(crate) mod tests;
