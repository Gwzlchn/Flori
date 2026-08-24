#[path = "daemon_extract_tests.rs"]
mod extract;
#[path = "daemon_failure_tests.rs"]
mod failures;
#[path = "daemon_test_support.rs"]
mod support;

use std::{net::TcpListener, time::Duration};

use flori_core::{
    ArtifactDeclaration, ArtifactKind, ArtifactWhen, AttemptId, ErrorCode, Executor, JobId,
    ResolvedSource, ResolvedSourceInput, ResolvedTaskInputs, SecretInputs, SourceId, SourceInputId,
    SourceKind, TaskClaim, TaskId,
};
use tokio::sync::watch;

use super::{PdfAcquireConfig, PdfDaemonConfig, PdfExtractConfig, run_pdf_daemon};
use crate::RunnerClient;
use support::{SuccessCase, TestRoot, digest, failure_server, poll_server, success_server};

#[tokio::test]
async fn upload_pdf_uses_authenticated_content_and_streams_output() {
    let root = TestRoot::new("acquire");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let mut pdf = b"%PDF-1.7\n".to_vec();
    pdf.resize(1024 * 1024 + 31, b'x');
    let input_id = SourceInputId::generate();
    let claim = claim(
        Executor::DocumentAcquire,
        ResolvedTaskInputs::DocumentAcquire {
            source: ResolvedSource {
                source_id: SourceId::generate(),
                kind: SourceKind::PdfUpload,
                canonical_ref: format!("upload:{input_id}"),
                input: Some(ResolvedSourceInput {
                    source_input_id: input_id,
                    name: "paper.pdf".into(),
                    media_type: "application/pdf".into(),
                    size_bytes: pdf.len() as u64,
                    sha256: digest(&pdf),
                    download_url: format!("{base}/api/v1/source-inputs/{input_id}/content"),
                }),
            },
        },
        acquire_declarations(),
    );
    let server = success_server(
        listener,
        SuccessCase {
            claim: claim.clone(),
            input: pdf.clone(),
            input_path: format!("/api/v1/source-inputs/{input_id}/content"),
            input_media_type: "application/pdf",
        },
    );
    let config = config(
        &root,
        root.script("pdfinfo", "printf 'Pages: 1\\n'"),
        root.script(
            "pdftotext",
            "printf 'this-page-has-more-than-thirty-two-visible-characters\\014'",
        ),
        root.script("python", "exit 1"),
    );
    run_until_server_closes(&base, &config, &server).await;
    let uploads = server.join().expect("server");
    assert_eq!(uploads.len(), 1);
    assert_eq!(uploads[0].name, "original");
    assert_eq!(uploads[0].bytes, pdf);
    assert_eq!(uploads[0].chunks, 2);
}

async fn run_until_server_closes<T>(
    base: &str,
    config: &PdfDaemonConfig,
    server: &std::thread::JoinHandle<T>,
) {
    let client = RunnerClient::new(base, "runner-token").expect("client");
    let (stop, mut cancel) = watch::channel(false);
    let mut daemon = Box::pin(run_pdf_daemon(&client, config, &mut cancel));
    tokio::time::timeout(Duration::from_secs(10), async {
        while !server.is_finished() {
            tokio::select! {
                result = &mut daemon => panic!("daemon stopped early: {result:?}"),
                () = tokio::time::sleep(Duration::from_millis(10)) => {},
            }
        }
    })
    .await
    .expect("server completion timeout");
    tokio::time::sleep(Duration::from_millis(50)).await;
    stop.send(true).expect("stop daemon");
    assert!(matches!(
        daemon.await,
        Ok(()) | Err(ErrorCode::TaskCanceled)
    ));
}

fn claim(
    executor: Executor,
    inputs: ResolvedTaskInputs,
    output_declarations: Vec<ArtifactDeclaration>,
) -> TaskClaim {
    TaskClaim {
        job_id: JobId::generate(),
        task_id: TaskId::generate(),
        task_key: "pdf".into(),
        exec_id: AttemptId::generate(),
        attempt_no: 1,
        executor,
        timeout_ms: 10_000,
        lease_expires_at_ms: now_ms() + 60_000,
        prompt_snapshot_sha256: digest(b"none"),
        resolved_inputs: inputs,
        output_declarations,
        model: None,
        effort: None,
        runner_config_revision: 1,
        secret_inputs: SecretInputs::default(),
    }
}

fn acquire_declarations() -> Vec<ArtifactDeclaration> {
    vec![
        declaration(
            "original",
            ArtifactKind::SourceOriginal,
            "output/source.pdf",
            true,
            None,
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "scholarly_html",
            ArtifactKind::ScholarlyHtml,
            "output/scholarly/document.html",
            false,
            None,
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "scholarly_snapshot",
            ArtifactKind::ScholarlyHtmlSnapshot,
            "output/scholarly/snapshot.json",
            false,
            None,
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "scholarly_resources",
            ArtifactKind::ScholarlyResource,
            "output/scholarly/resources/*",
            false,
            Some(256),
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "log",
            ArtifactKind::TaskLog,
            "logs/task.ndjson",
            true,
            None,
            ArtifactWhen::Always,
        ),
    ]
}

fn extract_declarations() -> Vec<ArtifactDeclaration> {
    vec![
        declaration(
            "structure",
            ArtifactKind::DocumentStructure,
            "output/document.json",
            true,
            None,
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "figures",
            ArtifactKind::Figure,
            "output/figures/*",
            false,
            Some(4),
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "tables",
            ArtifactKind::TableRegion,
            "output/tables/*",
            false,
            Some(4),
            ArtifactWhen::OnSuccess,
        ),
        declaration(
            "log",
            ArtifactKind::TaskLog,
            "logs/task.ndjson",
            true,
            None,
            ArtifactWhen::Always,
        ),
    ]
}

fn declaration(
    name: &str,
    kind: ArtifactKind,
    path: &str,
    required: bool,
    max_files: Option<u16>,
    when: ArtifactWhen,
) -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: name.into(),
        kind,
        path: path.into(),
        required,
        when,
        max_files,
        max_bytes: 2 * 1024 * 1024,
    }
}

fn config(
    root: &TestRoot,
    pdfinfo: std::path::PathBuf,
    pdftotext: std::path::PathBuf,
    python: std::path::PathBuf,
) -> PdfDaemonConfig {
    PdfDaemonConfig {
        work_root: root.0.join("work"),
        acquire: PdfAcquireConfig {
            pdfinfo,
            pdftotext,
            max_bytes: 2 * 1024 * 1024,
            max_probe_output_bytes: 4096,
            timeout: Duration::from_secs(2),
        },
        extract: PdfExtractConfig {
            python,
            timeout: Duration::from_secs(2),
            max_structure_bytes: 2 * 1024 * 1024,
            max_asset_bytes: 1024,
            max_assets: 8,
        },
        renew_interval: Duration::from_secs(30),
    }
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("time")
        .as_millis()
        .try_into()
        .expect("timestamp")
}
