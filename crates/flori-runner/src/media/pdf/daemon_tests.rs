#[path = "daemon_test_support.rs"]
mod support;

use std::{net::TcpListener, time::Duration};

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactWhen, AttemptId, ErrorCode, Executor,
    JobId, ResolvedArtifact, ResolvedSource, ResolvedSourceInput, ResolvedTaskInputs, SecretInputs,
    SourceId, SourceInputId, SourceKind, TaskClaim, TaskId,
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
            output_kind: ArtifactKind::SourceOriginal,
            output_media_type: "application/pdf",
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
    run_until_server_closes(&base, &config).await;
    assert_eq!(server.join().expect("server"), pdf);
}

#[tokio::test]
async fn extract_pdf_downloads_input_and_uploads_strict_structure() {
    let root = TestRoot::new("extract");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let pdf = b"%PDF-1.7\ndigital fixture".to_vec();
    let artifact_id = ArtifactId::generate();
    let claim = claim(
        Executor::DocumentExtract,
        ResolvedTaskInputs::DocumentExtract {
            pdf: ResolvedArtifact {
                artifact_id,
                name: "original".into(),
                kind: ArtifactKind::SourceOriginal,
                media_type: "application/pdf".into(),
                size_bytes: pdf.len() as u64,
                sha256: digest(&pdf),
                download_url: format!("{base}/api/v1/artifacts/{artifact_id}/content"),
            },
        },
        extract_declarations(),
    );
    let server = success_server(
        listener,
        SuccessCase {
            claim: claim.clone(),
            input: pdf,
            input_path: format!("/api/v1/artifacts/{artifact_id}/content"),
            input_media_type: "application/pdf",
            output_kind: ArtifactKind::DocumentStructure,
            output_media_type: "application/json",
        },
    );
    let python = root.script(
        "python",
        r#"output="$4"
id="$5"
cat > "$output/document.json" <<EOF
{"schema":"flori.document_structure.v1","source_artifact_id":"$id","language":"en","pages":[{"page":1,"width_pt":100.0,"height_pt":200.0}],"sections":[{"id":"s1","heading":"Intro","blocks":[{"page":1,"bbox":{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0},"text":"this-page-has-more-than-thirty-two-visible-characters"}]}],"figures":[],"tables":[]}
EOF"#,
    );
    let config = config(
        &root,
        root.script("unused-info", "exit 1"),
        root.script("unused-text", "exit 1"),
        python,
    );
    run_until_server_closes(&base, &config).await;
    let output = server.join().expect("server");
    let document: flori_core::DocumentStructure =
        serde_json::from_slice(&output).expect("strict structure");
    assert_eq!(document.source_artifact_id, artifact_id);
}

#[tokio::test]
async fn malformed_media_claim_fails_without_running_tools() {
    let root = TestRoot::new("invalid");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let mut claim = claim(
        Executor::DocumentAcquire,
        ResolvedTaskInputs::DocumentAcquire {
            source: ResolvedSource {
                source_id: SourceId::generate(),
                kind: SourceKind::PdfUrl,
                canonical_ref: "url:https://example.invalid/paper.pdf".into(),
                input: None,
            },
        },
        acquire_declarations(),
    );
    claim.model = Some("must-not-exist".into());
    let server = failure_server(listener, claim, ErrorCode::CorruptState);
    let config = config(
        &root,
        root.script("info", "exit 99"),
        root.script("text", "exit 99"),
        root.script("python", "exit 99"),
    );
    run_until_server_closes(&base, &config).await;
    server.join().expect("server");
}

#[tokio::test]
async fn expired_claim_stops_before_task_side_effects() {
    let root = TestRoot::new("expired");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let mut claim = claim(
        Executor::DocumentAcquire,
        ResolvedTaskInputs::DocumentAcquire {
            source: ResolvedSource {
                source_id: SourceId::generate(),
                kind: SourceKind::PdfUrl,
                canonical_ref: "url:https://example.invalid/paper.pdf".into(),
                input: None,
            },
        },
        acquire_declarations(),
    );
    claim.lease_expires_at_ms = now_ms() - 1;
    let server = poll_server(listener, claim);
    let config = config(
        &root,
        root.script("info", "exit 99"),
        root.script("text", "exit 99"),
        root.script("python", "exit 99"),
    );
    let client = RunnerClient::new(&base, "runner-token").expect("client");
    let (_keep, mut cancel) = watch::channel(false);
    assert_eq!(
        run_pdf_daemon(&client, &config, &mut cancel).await,
        Err(ErrorCode::LeaseExpired)
    );
    server.join().expect("server");
}

async fn run_until_server_closes(base: &str, config: &PdfDaemonConfig) {
    let client = RunnerClient::new(base, "runner-token").expect("client");
    let (_keep, mut cancel) = watch::channel(false);
    assert_eq!(
        run_pdf_daemon(&client, config, &mut cancel).await,
        Err(ErrorCode::NetworkTemporary)
    );
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
