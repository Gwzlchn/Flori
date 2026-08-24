use std::{ffi::OsString, fmt, path::PathBuf, time::Duration};

use flori_core::{AiTool, ErrorCode};
use reqwest::Url;
use tokio::sync::watch;

use crate::child_process::{ChildProcessConfig, ChildTermination, run_child_process};

#[path = "process/qoder_process.rs"]
#[cfg(feature = "qoder")]
pub(crate) mod qoder_process;

pub struct AiProcessConfig {
    pub tool: AiTool,
    pub executable: PathBuf,
    pub arguments: Vec<OsString>,
    pub home: PathBuf,
    pub tool_config_home: PathBuf,
    pub working_directory: PathBuf,
    pub timeout: Duration,
    pub max_output_bytes: usize,
    pub proxy_url: Url,
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
    pub(crate) const fn new(code: ErrorCode) -> Self {
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
    let config_name = match config.tool {
        #[cfg(feature = "qoder")]
        AiTool::QoderCli => "QODER_CONFIG_DIR",
        #[cfg(feature = "codex")]
        AiTool::CodexCli => "CODEX_HOME",
        #[allow(unreachable_patterns)]
        _ => return Err(AiProcessError::new(ErrorCode::InvalidRequest)),
    };
    let value = config.proxy_url.as_str().trim_end_matches('/');
    let environment = [
        ("PATH", OsString::from("/usr/local/bin:/usr/bin:/bin")),
        ("HOME", config.home.as_os_str().to_owned()),
        (config_name, config.tool_config_home.as_os_str().to_owned()),
        ("HTTP_PROXY", OsString::from(value)),
        ("http_proxy", OsString::from(value)),
        ("HTTPS_PROXY", OsString::from(value)),
        ("https_proxy", OsString::from(value)),
    ]
    .into_iter()
    .map(|(name, value)| (OsString::from(name), value))
    .collect();
    let output = run_child_process(
        &ChildProcessConfig {
            executable: config.executable.clone(),
            arguments: config.arguments.clone(),
            working_directory: config.working_directory.clone(),
            environment,
            stdin: Some(prompt.to_vec()),
            timeout: config.timeout,
            max_output_bytes: config.max_output_bytes,
        },
        cancel,
    )
    .await
    .map_err(AiProcessError::new)?;
    Ok(AiProcessOutput {
        stdout: output.stdout,
        stderr: output.stderr,
        exit_code: output.exit_code,
        termination: match output.termination {
            ChildTermination::Exited => AiProcessTermination::Exited,
            ChildTermination::TimedOut => AiProcessTermination::TimedOut,
            ChildTermination::Canceled => AiProcessTermination::Canceled,
        },
    })
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
            "printf 'argc=%s home=%s config=%s proxy=%s leak=%s\\n' \"$#\" \"$HOME\" ",
            "\"$CODEX_HOME\" \"$HTTPS_PROXY\" \"${AWS_SECRET_ACCESS_KEY-unset}\"; ",
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
        assert!(stdout.contains("proxy=http://codex-proxy.internal:10810"));
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

    #[tokio::test]
    async fn timeout_and_cancel_kill_background_children() {
        let root = TestDir::new();
        for (name, cancel_after) in [("timeout", None), ("cancel", Some(30))] {
            let marker = root.0.join(format!("{name}-survived"));
            let script = format!(
                "(sleep 0.15; touch '{}') & while :; do :; done",
                marker.display()
            );
            let timeout =
                cancel_after.map_or(Duration::from_millis(30), |_| Duration::from_secs(2));
            let (sender, mut receiver) = watch::channel(false);
            let cancel_task = cancel_after.map(|delay| {
                tokio::spawn(async move {
                    tokio::time::sleep(Duration::from_millis(delay)).await;
                    sender.send(true).expect("cancel");
                })
            });
            let output =
                run_ai_process(&config(&root.0, timeout, 4096, &script), b"", &mut receiver)
                    .await
                    .expect("stopped outcome");
            if let Some(task) = cancel_task {
                task.await.expect("cancel task");
            }
            tokio::time::sleep(Duration::from_millis(180)).await;
            assert!(!marker.exists(), "{name} left a background child");
            assert_ne!(output.termination, AiProcessTermination::Exited);
        }

        let marker = root.0.join("output-limit-survived");
        let script = format!(
            "(sleep 0.15; touch '{}') & while :; do printf 0123456789; done",
            marker.display()
        );
        let (_keep, mut receiver) = watch::channel(false);
        let error = run_ai_process(
            &config(&root.0, Duration::from_secs(2), 128, &script),
            b"",
            &mut receiver,
        )
        .await
        .expect_err("output limit");
        assert_eq!(error.code(), ErrorCode::ArtifactTooLarge);
        tokio::time::sleep(Duration::from_millis(180)).await;
        assert!(!marker.exists(), "output limit left a background child");

        let marker = root.0.join("aborted-future-survived");
        let script = format!(
            "(sleep 0.15; touch '{}') & while :; do :; done",
            marker.display()
        );
        let (_keep, receiver) = watch::channel(false);
        let abort_config = config(&root.0, Duration::from_secs(2), 4096, &script);
        let task = tokio::spawn(async move {
            let mut receiver = receiver;
            run_ai_process(&abort_config, b"", &mut receiver).await
        });
        tokio::time::sleep(Duration::from_millis(30)).await;
        task.abort();
        assert!(task.await.expect_err("aborted future").is_cancelled());
        tokio::time::sleep(Duration::from_millis(180)).await;
        assert!(!marker.exists(), "aborted future left a background child");

        let marker = root.0.join("stdin-error-survived");
        let script = format!(
            "exec 0<&-; (sleep 0.15; touch '{}') & while :; do :; done",
            marker.display()
        );
        let (_keep, mut receiver) = watch::channel(false);
        let prompt = vec![b'x'; 1024 * 1024];
        let error = run_ai_process(
            &config(&root.0, Duration::from_secs(2), 4096, &script),
            &prompt,
            &mut receiver,
        )
        .await
        .expect_err("stdin write error");
        assert_eq!(error.code(), ErrorCode::ExecutorFailed);
        tokio::time::sleep(Duration::from_millis(180)).await;
        assert!(!marker.exists(), "stdin error left a background child");

        let marker = root.0.join("clean-exit-survived");
        let script = format!("(sleep 0.15; touch '{}') & exit 0", marker.display());
        let (_keep, mut receiver) = watch::channel(false);
        let output = tokio::time::timeout(
            Duration::from_secs(1),
            run_ai_process(
                &config(&root.0, Duration::from_secs(2), 4096, &script),
                b"",
                &mut receiver,
            ),
        )
        .await
        .expect("clean exit did not hang")
        .expect("clean exit outcome");
        assert_eq!(output.termination, AiProcessTermination::Exited);
        assert_eq!(output.exit_code, Some(0));
        tokio::time::sleep(Duration::from_millis(180)).await;
        assert!(!marker.exists(), "clean exit left a background child");
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
            proxy_url: Url::parse("http://codex-proxy.internal:10810").expect("Codex proxy"),
        }
    }
}
