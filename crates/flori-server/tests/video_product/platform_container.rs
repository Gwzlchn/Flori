use std::{env, ffi::OsString, net::SocketAddr, process::Output, time::Duration};

use flori_core::RequestId;
use tokio::{process::Command, task::JoinHandle};

pub(super) struct RunningContainer {
    name: String,
    task: Option<JoinHandle<std::io::Result<Output>>>,
    active: bool,
}

pub(super) async fn start(address: SocketAddr, token: String) -> RunningContainer {
    let proxy = env::var_os("FLORI_YOUTUBE_PROXY_URL")
        .expect("FLORI_YOUTUBE_PROXY_URL is required for external video acceptance");
    let image = env::var_os("FLORI_RUNNER_DOWNLOAD_IMAGE")
        .unwrap_or_else(|| OsString::from("flori-runner-download:local"));
    let name = format!("flori-wp12c-download-{}", RequestId::generate());
    let server = format!("http://127.0.0.1:{}/", address.port());
    let mut command = Command::new("docker");
    command
        .args(["run", "--rm", "--name", &name, "--network", "host"])
        .args([
            "--tmpfs",
            "/var/lib/flori-runner:rw,uid=65532,gid=65532,mode=0700",
        ])
        .args(["--env", "FLORI_SERVER_URL"])
        .args(["--env", "FLORI_RUNNER_TOKEN"])
        .args(["--env", "FLORI_YOUTUBE_PROXY_URL"])
        .arg(image)
        .env("FLORI_SERVER_URL", server)
        .env("FLORI_RUNNER_TOKEN", token)
        .env("FLORI_YOUTUBE_PROXY_URL", proxy);
    let task = tokio::spawn(async move { command.output().await });
    RunningContainer {
        name,
        task: Some(task),
        active: true,
    }
}

impl RunningContainer {
    pub(super) async fn stop(mut self) {
        let _stopped = Command::new("docker")
            .args(["kill", "--signal=INT", &self.name])
            .status()
            .await
            .expect("stop download container");
        let output = tokio::time::timeout(
            Duration::from_secs(20),
            self.task.take().expect("download task"),
        )
        .await
        .expect("download container stop timeout")
        .expect("download task join")
        .expect("download container process");
        self.active = false;
        assert!(
            output.status.success(),
            "download container failed: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

impl Drop for RunningContainer {
    fn drop(&mut self) {
        if self.active {
            let _ = std::process::Command::new("docker")
                .args(["kill", &self.name])
                .status();
        }
    }
}
