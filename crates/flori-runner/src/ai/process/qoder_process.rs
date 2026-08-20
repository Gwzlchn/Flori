use std::{
    fs,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::{Path, PathBuf},
};

use flori_core::{AiTool, ErrorCode};
use tokio::sync::watch;

use super::{AiProcessConfig, AiProcessError, AiProcessOutput, run_ai_process};

const PRIVATE_HOME: &str = ".qoder-home";

pub(crate) async fn run(
    mut config: AiProcessConfig,
    prompt: &[u8],
    cancel: &mut watch::Receiver<bool>,
) -> Result<AiProcessOutput, AiProcessError> {
    if config.tool != AiTool::QoderCli {
        return Err(AiProcessError::new(ErrorCode::InvalidRequest));
    }
    let home = PrivateHome::create(&config.working_directory)?;
    config.home = home.path().to_owned();
    run_ai_process(&config, prompt, cancel).await
}

struct PrivateHome(PathBuf);

impl PrivateHome {
    fn create(workspace: &Path) -> Result<Self, AiProcessError> {
        let path = workspace.join(PRIVATE_HOME);
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|_| AiProcessError::new(ErrorCode::StorageUnavailable))?;
        let home = Self(path);
        fs::set_permissions(&home.0, fs::Permissions::from_mode(0o700))
            .map_err(|_| AiProcessError::new(ErrorCode::StorageUnavailable))?;
        Ok(home)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for PrivateHome {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
mod tests {
    use std::{ffi::OsString, time::Duration};

    use flori_core::RequestId;
    use reqwest::Url;
    use tokio::time::sleep;

    use super::*;
    use crate::AiProcessTermination;

    struct TestDir(PathBuf);

    impl TestDir {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("flori-qoder-{}", RequestId::generate()));
            for child in ["home", "config"] {
                fs::create_dir_all(root.join(child)).expect("test directory");
            }
            Self(root)
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).expect("cleanup test directory");
        }
    }

    #[tokio::test]
    async fn injects_only_proxy_variables_and_cleans_private_home() {
        let root = TestDir::new();
        let script = concat!(
            "{ printf 'home=%s\\n' \"$HOME\"; ",
            "printf 'config=%s\\n' \"$QODER_CONFIG_DIR\"; ",
            "printf 'mode=%s\\n' \"$(stat -c %a \"$HOME\")\"; ",
            "printf 'http=%s\\n' \"$HTTP_PROXY\"; ",
            "printf 'http_lower=%s\\n' \"$http_proxy\"; ",
            "printf 'https=%s\\n' \"$HTTPS_PROXY\"; ",
            "printf 'https_lower=%s\\n' \"$https_proxy\"; ",
            "printf 'all=%s\\n' \"${ALL_PROXY-unset}\"; ",
            "printf 'aws=%s\\n' \"${AWS_SECRET_ACCESS_KEY-unset}\"; ",
            "} > qoder-environment; ",
            "mkdir -p \"$HOME/.config/Qoder/qodercli\"; ",
            "printf sensitive-cache > \"$HOME/.config/Qoder/qodercli/cache\"; ",
            "printf safe-output"
        );
        let config = config(&root.0, "/bin/sh", script);
        let (_keep, mut cancel) = watch::channel(false);
        let output = run(config, b"prompt\n", &mut cancel)
            .await
            .expect("Qoder process");

        assert_eq!(output.stdout, b"safe-output");
        let report = fs::read_to_string(root.0.join("qoder-environment")).expect("environment");
        assert!(report.contains(&format!("home={}", root.0.join(PRIVATE_HOME).display())));
        assert!(report.contains(&format!("config={}", root.0.join("config").display())));
        assert!(report.contains("mode=700"));
        for name in ["http", "http_lower", "https", "https_lower"] {
            assert!(report.contains(&format!("{name}=http://proxy.internal:10809")));
        }
        assert!(report.contains("all=unset"));
        assert!(report.contains("aws=unset"));
        assert!(!root.0.join(PRIVATE_HOME).exists());
        assert!(!String::from_utf8_lossy(&output.stdout).contains("proxy.internal"));
    }

    #[tokio::test]
    async fn spawn_failure_also_cleans_private_home() {
        let root = TestDir::new();
        let config = config(&root.0, "/does/not/exist", "");
        let (_keep, mut cancel) = watch::channel(false);
        let error = run(config, b"", &mut cancel)
            .await
            .expect_err("spawn error");
        assert_eq!(error.code(), ErrorCode::ExecutorFailed);
        assert!(!root.0.join(PRIVATE_HOME).exists());
    }

    #[tokio::test]
    async fn cancellation_also_cleans_private_home() {
        let root = TestDir::new();
        let config = config(&root.0, "/bin/sh", "while :; do :; done");
        let (stop, mut cancel) = watch::channel(false);
        let cancel_task = tokio::spawn(async move {
            sleep(Duration::from_millis(30)).await;
            stop.send(true).expect("cancel process");
        });
        let output = run(config, b"", &mut cancel)
            .await
            .expect("canceled process outcome");
        cancel_task.await.expect("cancel task");
        assert_eq!(output.termination, AiProcessTermination::Canceled);
        assert!(!root.0.join(PRIVATE_HOME).exists());
    }

    fn config(root: &Path, executable: &str, script: &str) -> AiProcessConfig {
        AiProcessConfig {
            tool: AiTool::QoderCli,
            executable: executable.into(),
            arguments: vec![OsString::from("-c"), OsString::from(script)],
            home: root.join("home"),
            tool_config_home: root.join("config"),
            working_directory: root.to_owned(),
            timeout: Duration::from_secs(2),
            max_output_bytes: 4096,
            proxy_url: Some(Url::parse("http://proxy.internal:10809").expect("proxy")),
        }
    }
}
