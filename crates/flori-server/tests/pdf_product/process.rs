use std::{
    fs::{self, OpenOptions},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
};

use flori_core::{
    CreateJobRequest, CreateUploadSource, CreatedJob, DocumentStructure, ErrorCode, JobId,
    JobInputs, SourceId, SourceKind,
};
use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use tokio::{sync::watch, task::JoinHandle};

use super::{Harness, assertions, fixture, http, wait_for_task};

pub(super) struct Ingested {
    pub(super) source_id: SourceId,
    pub(super) job_id: JobId,
    pub(super) media_token: String,
    pub(super) qoder_token: String,
    pub(super) media_log: PathBuf,
    pub(super) document: DocumentStructure,
}

impl Harness {
    pub(super) async fn ingest(
        &self,
        image: &str,
        pdf_path: &Path,
        model: &str,
        effort: &str,
    ) -> Ingested {
        let pdf = fs::read(pdf_path).expect("read digital PDF");
        let created = http::upload_pdf(
            self.address,
            &CreateUploadSource {
                request_key: "pdf-product-upload".into(),
                kind: SourceKind::PdfUpload,
                title: Some("Flori vNext PDF Product Acceptance".into()),
                domain_id: self.domain_id,
                collection_ids: Vec::new(),
                file_sha256: fixture::digest(&pdf),
            },
            pdf_path
                .file_name()
                .and_then(|name| name.to_str())
                .expect("PDF filename must be UTF-8"),
            &pdf,
        )
        .await;
        let job: CreatedJob = http::post_json(
            self.address,
            &format!("/api/v1/sources/{}/jobs", created.source_id),
            &CreateJobRequest {
                request_key: "pdf-product-job".into(),
                pipeline_id: self.pipeline_id,
                inputs: JobInputs { translate: false },
            },
        )
        .await;
        let media = RunnerClient::register(
            &self.base_url(),
            fixture::MEDIA_REGISTRATION,
            &fixture::media_capabilities(),
        )
        .await
        .expect("register media Runner");
        assert_eq!(media.runner_id, self.media_runner_id);
        let qoder = RunnerClient::register(
            &self.base_url(),
            fixture::QODER_REGISTRATION,
            &fixture::qoder_capabilities(model, effort),
        )
        .await
        .expect("register Qoder Runner");
        assert_eq!(qoder.runner_id, self.qoder_runner_id);
        let mut media_process =
            DockerRunner::start(&self.root, image, &self.base_url(), &media.token);
        wait_for_task(&self.pool, job.job_id, "extract", &mut media_process).await;
        let document = assertions::load_document(&self.pool, &self.artifact_root, job.job_id).await;
        let media_log = media_process.log_path.clone();
        drop(media_process);
        Ingested {
            source_id: created.source_id,
            job_id: job.job_id,
            media_token: media.token,
            qoder_token: qoder.token,
            media_log,
            document,
        }
    }
}

pub(super) struct DockerRunner {
    name: String,
    child: Option<Child>,
    environment: PathBuf,
    pub(super) log_path: PathBuf,
}

impl DockerRunner {
    pub(super) fn start(root: &Path, image: &str, server_url: &str, token: &str) -> Self {
        let name = format!("flori-pdf-product-{}", flori_core::RequestId::generate());
        let environment = root.join("media-runner.env");
        fs::write(
            &environment,
            format!(
                "FLORI_SERVER_URL={server_url}\nFLORI_RUNNER_TOKEN={token}\n\
                 FLORI_RUNNER_SPOOL_DIR=/var/lib/flori-runner/spool\n"
            ),
        )
        .expect("write media runner environment");
        fs::set_permissions(&environment, fs::Permissions::from_mode(0o600))
            .expect("protect media runner environment");
        let spool = root.join("media-spool");
        fs::create_dir(&spool).expect("create media spool");
        fs::set_permissions(&spool, fs::Permissions::from_mode(0o777))
            .expect("make media spool writable by the container user");
        let log_path = root.join("media-runner.log");
        let stdout = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&log_path)
            .expect("create media runner log");
        let stderr = stdout.try_clone().expect("clone media runner log");
        let child = Command::new("docker")
            .args(["run", "--rm", "--name", &name, "--network", "host"])
            .arg("--env-file")
            .arg(&environment)
            .arg("--volume")
            .arg(format!("{}:/var/lib/flori-runner/spool", spool.display()))
            .arg(image)
            .args(["run", "media"])
            .stdin(Stdio::null())
            .stdout(Stdio::from(stdout))
            .stderr(Stdio::from(stderr))
            .spawn()
            .expect("start runner-media Docker container");
        Self {
            name,
            child: Some(child),
            environment,
            log_path,
        }
    }

    pub(super) fn assert_running(&mut self) {
        let Some(status) = self
            .child
            .as_mut()
            .expect("Docker child")
            .try_wait()
            .expect("inspect runner-media process")
        else {
            return;
        };
        let log = fs::read_to_string(&self.log_path).unwrap_or_default();
        panic!("runner-media exited early with {status}: {log}");
    }

    fn terminate(&mut self) {
        let _ = Command::new("docker")
            .args(["rm", "--force", &self.name])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::remove_file(&self.environment);
    }
}

impl Drop for DockerRunner {
    fn drop(&mut self) {
        self.terminate();
    }
}

pub(super) struct AiDaemon {
    cancel: watch::Sender<bool>,
    task: Option<JoinHandle<Result<(), ErrorCode>>>,
}

impl AiDaemon {
    pub(super) fn start(client: RunnerClient, config: DaemonConfig) -> Self {
        let (cancel, mut receiver) = watch::channel(false);
        let task =
            tokio::spawn(async move { run_ai_daemon(&client, &config, &mut receiver).await });
        Self {
            cancel,
            task: Some(task),
        }
    }

    pub(super) async fn stop(mut self) {
        let _ = self.cancel.send(true);
        let result = self
            .task
            .take()
            .expect("AI daemon task")
            .await
            .expect("AI daemon join");
        assert!(
            matches!(result, Ok(()) | Err(ErrorCode::TaskCanceled)),
            "AI daemon stopped unexpectedly: {result:?}"
        );
    }

    pub(super) fn assert_running(&self) {
        assert!(
            !self.task.as_ref().expect("AI daemon task").is_finished(),
            "AI daemon exited before the Job reached a terminal state"
        );
    }
}

impl Drop for AiDaemon {
    fn drop(&mut self) {
        let _ = self.cancel.send(true);
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}
