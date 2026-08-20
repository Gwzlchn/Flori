use std::{
    ffi::OsString,
    fmt,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use flori_core::{AiTool, ErrorCode};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWriteExt},
    process::Command,
    sync::watch,
};

const SAFE_PATH: &str = "/usr/local/bin:/usr/bin:/bin";

pub struct AiProcessConfig {
    pub tool: AiTool,
    pub executable: PathBuf,
    pub arguments: Vec<OsString>,
    pub home: PathBuf,
    pub tool_config_home: PathBuf,
    pub working_directory: PathBuf,
    pub timeout: Duration,
    pub max_output_bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AiProcessTermination {
    Exited,
    TimedOut,
    Canceled,
}

#[derive(Debug, Eq, PartialEq)]
pub struct AiProcessOutput {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub exit_code: Option<i32>,
    pub termination: AiProcessTermination,
}

#[derive(Debug)]
pub struct AiProcessError {
    code: ErrorCode,
}

impl AiProcessError {
    const fn new(code: ErrorCode) -> Self {
        Self { code }
    }

    #[must_use]
    pub const fn code(&self) -> ErrorCode {
        self.code
    }
}

impl fmt::Display for AiProcessError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "AI process failed: {:?}", self.code)
    }
}

impl std::error::Error for AiProcessError {}

pub async fn run_ai_process(
    config: &AiProcessConfig,
    prompt: &[u8],
    cancel: &mut watch::Receiver<bool>,
) -> Result<AiProcessOutput, AiProcessError> {
    validate(config)?;
    let mut command = Command::new(&config.executable);
    command
        .args(&config.arguments)
        .current_dir(&config.working_directory)
        .env_clear()
        .env("PATH", SAFE_PATH)
        .env("HOME", &config.home)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    match config.tool {
        AiTool::QoderCli => command.env("QODER_CONFIG_DIR", &config.tool_config_home),
        AiTool::CodexCli => command.env("CODEX_HOME", &config.tool_config_home),
    };
    let mut child = command
        .spawn()
        .map_err(|_| AiProcessError::new(ErrorCode::ExecutorFailed))?;
    let mut stdin = child
        .stdin
        .take()
        .ok_or_else(|| AiProcessError::new(ErrorCode::Internal))?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| AiProcessError::new(ErrorCode::Internal))?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| AiProcessError::new(ErrorCode::Internal))?;
    let prompt = prompt.to_vec();
    let prompt_task = tokio::spawn(async move {
        stdin.write_all(&prompt).await?;
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
        Exited(Result<std::process::ExitStatus, ()>),
        TimedOut,
        Canceled,
        OutputLimit,
    }
    let stop = tokio::select! {
        status = async {
            prompt_task.await.map_err(|_| ())?.map_err(|_| ())?;
            child.wait().await.map_err(|_| ())
        } => Stop::Exited(status),
        () = canceled(cancel) => Stop::Canceled,
        () = tokio::time::sleep(config.timeout) => Stop::TimedOut,
        result = limit_rx.changed() => {
            let _ = result;
            Stop::OutputLimit
        }
    };
    let (termination, status) = match stop {
        Stop::Exited(Ok(status)) => (AiProcessTermination::Exited, status),
        Stop::Canceled => (
            AiProcessTermination::Canceled,
            kill_and_wait(&mut child).await?,
        ),
        Stop::TimedOut => (
            AiProcessTermination::TimedOut,
            kill_and_wait(&mut child).await?,
        ),
        Stop::OutputLimit => {
            kill_and_wait(&mut child).await?;
            let _ = stdout_task.await;
            let _ = stderr_task.await;
            return Err(AiProcessError::new(ErrorCode::ArtifactTooLarge));
        }
        Stop::Exited(Err(())) => {
            kill_and_wait(&mut child).await?;
            let _ = stdout_task.await;
            let _ = stderr_task.await;
            return Err(AiProcessError::new(ErrorCode::ExecutorFailed));
        }
    };
    let stdout = join_reader(stdout_task).await?;
    let stderr = join_reader(stderr_task).await?;
    Ok(AiProcessOutput {
        stdout,
        stderr,
        exit_code: status.code(),
        termination,
    })
}

async fn read_bounded(
    mut reader: impl AsyncRead + Unpin,
    max: usize,
    total: Arc<AtomicUsize>,
    limit: watch::Sender<bool>,
) -> Result<Vec<u8>, AiProcessError> {
    let mut output = Vec::new();
    let mut buffer = [0_u8; 8192];
    loop {
        let read = reader
            .read(&mut buffer)
            .await
            .map_err(|_| AiProcessError::new(ErrorCode::ExecutorFailed))?;
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
            return Err(AiProcessError::new(ErrorCode::ArtifactTooLarge));
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

async fn kill_and_wait(
    child: &mut tokio::process::Child,
) -> Result<std::process::ExitStatus, AiProcessError> {
    child
        .start_kill()
        .map_err(|_| AiProcessError::new(ErrorCode::ExecutorFailed))?;
    let status = child
        .wait()
        .await
        .map_err(|_| AiProcessError::new(ErrorCode::ExecutorFailed))?;
    Ok(status)
}

async fn join_reader(
    task: tokio::task::JoinHandle<Result<Vec<u8>, AiProcessError>>,
) -> Result<Vec<u8>, AiProcessError> {
    task.await
        .map_err(|_| AiProcessError::new(ErrorCode::Internal))?
}

fn validate(config: &AiProcessConfig) -> Result<(), AiProcessError> {
    if !config.executable.is_absolute()
        || !config.home.is_absolute()
        || !config.tool_config_home.is_absolute()
        || !config.working_directory.is_absolute()
        || config.timeout.is_zero()
        || config.max_output_bytes == 0
    {
        return Err(AiProcessError::new(ErrorCode::InvalidRequest));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::{fs, path::Path};

    use flori_core::RequestId;

    use super::*;

    struct TestDir(PathBuf);

    impl TestDir {
        fn new() -> Self {
            let path =
                std::env::temp_dir().join(format!("flori-process-{}", RequestId::generate()));
            fs::create_dir_all(path.join("home")).expect("home");
            fs::create_dir(path.join("config")).expect("config");
            Self(path)
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).expect("cleanup");
        }
    }

    #[tokio::test]
    async fn prompt_is_stdin_and_environment_is_explicit() {
        let root = TestDir::new();
        let script = concat!(
            "printf 'argc=%s home=%s config=%s leak=%s\\n' \"$#\" \"$HOME\" ",
            "\"$CODEX_HOME\" \"${AWS_SECRET_ACCESS_KEY-unset}\"; ",
            "IFS= read -r value; printf 'prompt-bytes=%s' \"${#value}\""
        );
        let config = config(&root.0, Duration::from_secs(2), 4096, script);
        let (_cancel, mut receiver) = watch::channel(false);
        let output = run_ai_process(&config, b"TOP_SECRET_PROMPT\n", &mut receiver)
            .await
            .expect("process");
        assert_eq!(output.termination, AiProcessTermination::Exited);
        assert_eq!(output.exit_code, Some(0));
        let stdout = String::from_utf8(output.stdout).expect("stdout");
        assert!(stdout.contains("argc=0"));
        assert!(stdout.contains(&format!("home={}", root.0.join("home").display())));
        assert!(stdout.contains(&format!("config={}", root.0.join("config").display())));
        assert!(stdout.contains("leak=unset"));
        assert!(stdout.contains("prompt-bytes=17"));
        assert!(!stdout.contains("TOP_SECRET_PROMPT"));
    }

    #[tokio::test]
    async fn reports_nonzero_timeout_cancel_and_output_limit() {
        let root = TestDir::new();
        let (_keep, mut receiver) = watch::channel(false);
        let nonzero = run_ai_process(
            &config(
                &root.0,
                Duration::from_secs(2),
                4096,
                "printf out; printf err >&2; exit 7",
            ),
            b"",
            &mut receiver,
        )
        .await
        .expect("nonzero outcome");
        assert_eq!(nonzero.exit_code, Some(7));
        assert_eq!(
            (nonzero.stdout.as_slice(), nonzero.stderr.as_slice()),
            (&b"out"[..], &b"err"[..])
        );

        let (_keep, mut receiver) = watch::channel(false);
        let timed_out = run_ai_process(
            &config(
                &root.0,
                Duration::from_millis(30),
                4096,
                "while :; do :; done",
            ),
            b"",
            &mut receiver,
        )
        .await
        .expect("timeout outcome");
        assert_eq!(timed_out.termination, AiProcessTermination::TimedOut);

        let (cancel, mut receiver) = watch::channel(false);
        let cancel_task = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(30)).await;
            cancel.send(true).expect("cancel");
        });
        let canceled = run_ai_process(
            &config(&root.0, Duration::from_secs(2), 4096, "while :; do :; done"),
            b"",
            &mut receiver,
        )
        .await
        .expect("cancel outcome");
        cancel_task.await.expect("cancel task");
        assert_eq!(canceled.termination, AiProcessTermination::Canceled);

        let (_keep, mut receiver) = watch::channel(false);
        let error = run_ai_process(
            &config(
                &root.0,
                Duration::from_secs(2),
                128,
                "while :; do printf 0123456789; done",
            ),
            b"",
            &mut receiver,
        )
        .await
        .expect_err("output limit");
        assert_eq!(error.code(), ErrorCode::ArtifactTooLarge);
    }

    fn config(
        root: &Path,
        timeout: Duration,
        max_output_bytes: usize,
        script: &str,
    ) -> AiProcessConfig {
        AiProcessConfig {
            tool: AiTool::CodexCli,
            executable: "/bin/sh".into(),
            arguments: vec!["-c".into(), script.into()],
            home: root.join("home"),
            tool_config_home: root.join("config"),
            working_directory: root.to_owned(),
            timeout,
            max_output_bytes,
        }
    }
}
