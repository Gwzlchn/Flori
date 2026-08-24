use std::{
    ffi::OsString,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use flori_core::ErrorCode;
use rustix::process::{Pid, Signal, WaitId, WaitIdOptions, kill_process_group, waitid};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::watch,
};

pub(crate) struct ChildProcessConfig {
    pub(crate) executable: PathBuf,
    pub(crate) arguments: Vec<OsString>,
    pub(crate) working_directory: PathBuf,
    pub(crate) environment: Vec<(OsString, OsString)>,
    pub(crate) stdin: Option<Vec<u8>>,
    pub(crate) timeout: Duration,
    pub(crate) max_output_bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ChildTermination {
    Exited,
    TimedOut,
    Canceled,
}

pub(crate) struct ChildOutput {
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
    pub(crate) exit_code: Option<i32>,
    pub(crate) termination: ChildTermination,
}

pub(crate) async fn run_child_process(
    config: &ChildProcessConfig,
    cancel: &mut watch::Receiver<bool>,
) -> Result<ChildOutput, ErrorCode> {
    validate(config)?;
    let mut command = Command::new(&config.executable);
    command
        .args(&config.arguments)
        .current_dir(&config.working_directory)
        .env_clear()
        .envs(config.environment.iter().cloned())
        .stdin(if config.stdin.is_some() {
            std::process::Stdio::piped()
        } else {
            std::process::Stdio::null()
        })
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true)
        .process_group(0);
    let mut child = command.spawn().map_err(|_| ErrorCode::ExecutorFailed)?;
    let mut process_group = ProcessGroup::new(&child)?;
    let stdin = child.stdin.take();
    let stdout = child.stdout.take().ok_or(ErrorCode::Internal)?;
    let stderr = child.stderr.take().ok_or(ErrorCode::Internal)?;
    let input = config.stdin.clone();
    let input_task = tokio::spawn(async move {
        let Some(input) = input else {
            return Ok::<(), std::io::Error>(());
        };
        let mut stdin = stdin.ok_or_else(|| std::io::Error::other("missing child stdin"))?;
        stdin.write_all(&input).await?;
        stdin.shutdown().await
    });
    let total = Arc::new(AtomicUsize::new(0));
    let (limit_tx, mut limit_rx) = watch::channel(false);
    let _limit_guard = limit_tx.clone();
    let stdout_task = tokio::spawn(read_bounded(
        stdout,
        config.max_output_bytes,
        total.clone(),
        limit_tx.clone(),
    ));
    let stderr_task = tokio::spawn(read_bounded(
        stderr,
        config.max_output_bytes,
        total,
        limit_tx,
    ));

    enum Stop {
        Exited(Result<(), ()>),
        TimedOut,
        Canceled,
        OutputLimit,
    }
    let stop = tokio::select! {
        status = async {
            input_task.await.map_err(|_| ())?.map_err(|_| ())?;
            process_exited(process_group.pid).await
        } => Stop::Exited(status),
        () = canceled(cancel) => Stop::Canceled,
        () = tokio::time::sleep(config.timeout) => Stop::TimedOut,
        result = limit_rx.changed() => {
            let _ = result;
            Stop::OutputLimit
        }
    };
    let (termination, status) = match stop {
        Stop::Exited(Ok(())) => (
            ChildTermination::Exited,
            kill_and_wait(&mut child, &mut process_group).await?,
        ),
        Stop::Canceled => (
            ChildTermination::Canceled,
            kill_and_wait(&mut child, &mut process_group).await?,
        ),
        Stop::TimedOut => (
            ChildTermination::TimedOut,
            kill_and_wait(&mut child, &mut process_group).await?,
        ),
        Stop::OutputLimit => {
            kill_and_wait(&mut child, &mut process_group).await?;
            let _ = stdout_task.await;
            let _ = stderr_task.await;
            return Err(ErrorCode::ArtifactTooLarge);
        }
        Stop::Exited(Err(())) => {
            kill_and_wait(&mut child, &mut process_group).await?;
            let _ = stdout_task.await;
            let _ = stderr_task.await;
            return Err(ErrorCode::ExecutorFailed);
        }
    };
    Ok(ChildOutput {
        stdout: join_reader(stdout_task).await?,
        stderr: join_reader(stderr_task).await?,
        exit_code: status.code(),
        termination,
    })
}

async fn read_bounded(
    mut reader: impl AsyncRead + Unpin,
    max: usize,
    total: Arc<AtomicUsize>,
    limit: watch::Sender<bool>,
) -> Result<Vec<u8>, ErrorCode> {
    let mut output = Vec::new();
    let mut buffer = [0_u8; 8_192];
    loop {
        let read = reader
            .read(&mut buffer)
            .await
            .map_err(|_| ErrorCode::ExecutorFailed)?;
        if read == 0 {
            return Ok(output);
        }
        if total
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_add(read).filter(|next| *next <= max)
            })
            .is_err()
        {
            let _ = limit.send(true);
            return Err(ErrorCode::ArtifactTooLarge);
        }
        output.extend_from_slice(&buffer[..read]);
    }
}

async fn canceled(cancel: &mut watch::Receiver<bool>) {
    loop {
        if *cancel.borrow() || cancel.changed().await.is_err() {
            return;
        }
    }
}

async fn process_exited(pid: Pid) -> Result<(), ()> {
    let options = WaitIdOptions::EXITED | WaitIdOptions::NOWAIT | WaitIdOptions::NOHANG;
    loop {
        match waitid(WaitId::Pid(pid), options) {
            Ok(Some(_)) => return Ok(()),
            Ok(None) => tokio::time::sleep(Duration::from_millis(5)).await,
            Err(_) => return Err(()),
        }
    }
}

async fn kill_and_wait(
    child: &mut tokio::process::Child,
    process_group: &mut ProcessGroup,
) -> Result<std::process::ExitStatus, ErrorCode> {
    let signal = process_group.kill_remaining();
    if signal.is_err() {
        let _ = child.start_kill();
    }
    let status = child.wait().await.map_err(|_| ErrorCode::ExecutorFailed)?;
    signal?;
    process_group.disarm();
    Ok(status)
}

struct ProcessGroup {
    pid: Pid,
    armed: bool,
}

impl ProcessGroup {
    fn new(child: &tokio::process::Child) -> Result<Self, ErrorCode> {
        let pid = child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .and_then(Pid::from_raw)
            .ok_or(ErrorCode::Internal)?;
        Ok(Self { pid, armed: true })
    }

    fn kill_remaining(&self) -> Result<(), ErrorCode> {
        match kill_process_group(self.pid, Signal::KILL) {
            Ok(()) | Err(rustix::io::Errno::SRCH) => Ok(()),
            Err(_) => Err(ErrorCode::ExecutorFailed),
        }
    }

    fn disarm(&mut self) {
        self.armed = false;
    }
}

impl Drop for ProcessGroup {
    fn drop(&mut self) {
        if self.armed {
            let _ = self.kill_remaining();
        }
    }
}

async fn join_reader(
    task: tokio::task::JoinHandle<Result<Vec<u8>, ErrorCode>>,
) -> Result<Vec<u8>, ErrorCode> {
    task.await.map_err(|_| ErrorCode::Internal)?
}

fn validate(config: &ChildProcessConfig) -> Result<(), ErrorCode> {
    if !config.executable.is_absolute()
        || !config.working_directory.is_absolute()
        || config.timeout.is_zero()
        || config.max_output_bytes == 0
    {
        return Err(ErrorCode::InvalidRequest);
    }
    let mut names = config
        .environment
        .iter()
        .map(|(name, _)| name.to_str().ok_or(ErrorCode::InvalidRequest))
        .collect::<Result<Vec<_>, _>>()?;
    names.sort_unstable();
    if names.iter().any(|name| {
        name.is_empty()
            || !name
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'_')
    }) || names.windows(2).any(|pair| pair[0] == pair[1])
    {
        return Err(ErrorCode::InvalidRequest);
    }
    Ok(())
}
