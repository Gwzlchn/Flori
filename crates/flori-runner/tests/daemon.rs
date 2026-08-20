#[path = "support/daemon.rs"]
mod support;

use std::{fs, net::TcpListener, thread, time::Duration};

use flori_core::{
    AiAudit, AiUsageId, AiUsageState, ArtifactId, ArtifactKind, ArtifactManifestEntry,
    ArtifactWhen, AttemptAck, AttemptId, AttemptState, DocumentPage, DocumentSection,
    DocumentStructure, DocumentStructureSchema, DocumentTextBlock, Executor, PdfRect,
    ResolvedTaskInputs, StartUploadRequest, StartUploadResponse, TaskClaim, UploadCursor, UploadId,
    UsageAck, VerifyUploadResponse,
};
use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use tokio::sync::watch;

use support::*;

#[tokio::test]
async fn replayed_usage_never_spawns_and_daemon_continues_after_failure() {
    let root = temp_root("usage-replay");
    let marker = root.join("spawned");
    let executable = script(
        &root,
        "fake-qoder",
        &format!("touch '{}'\n", marker.display()),
    );
    let document_id = ArtifactId::generate();
    let document = serde_json::to_vec(&DocumentStructure {
        schema: DocumentStructureSchema::V1,
        source_artifact_id: document_id,
        language: "en".into(),
        pages: vec![DocumentPage {
            page: 1,
            width_pt: 100.0,
            height_pt: 100.0,
        }],
        sections: vec![DocumentSection {
            id: "section-1".into(),
            heading: "Source".into(),
            blocks: vec![DocumentTextBlock {
                page: 1,
                bbox: PdfRect {
                    x1: 0.0,
                    y1: 0.0,
                    x2: 100.0,
                    y2: 20.0,
                },
                text: "source fact".into(),
            }],
        }],
        figures: vec![],
        tables: vec![],
    })
    .expect("document");
    let exec_id = AttemptId::generate();
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base_url = format!("http://{}", listener.local_addr().expect("address"));
    let claim = claim(
        exec_id,
        Executor::AiDocumentNote,
        ResolvedTaskInputs::AiDocumentNote {
            document: artifact_with_id(&base_url, &document, document_id),
            prompt: prompt("write a note"),
            profile: None,
        },
        vec![declaration(
            "audit",
            ArtifactKind::AiAudit,
            ArtifactWhen::Always,
        )],
        2_000,
    );
    let upload_id = UploadId::generate();
    let server = replay_server(
        listener,
        claim,
        document.clone(),
        digest(&document),
        exec_id,
        upload_id,
    );
    let client = RunnerClient::new(&base_url, "runner-token").expect("client");
    let config = config(&root, executable, Duration::from_secs(1));
    let (_keep, mut cancel) = watch::channel(false);
    assert_eq!(
        run_ai_daemon(&client, &config, &mut cancel).await,
        Err(flori_core::ErrorCode::NetworkTemporary)
    );
    let audit = server.join().expect("server");
    assert!(audit.usage_invocation_keys.is_empty());
    assert!(audit.redacted_arguments.is_empty());
    assert!(!marker.exists(), "idempotent replay must not spawn CLI");
    fs::remove_dir_all(root).expect("cleanup");
}

fn replay_server(
    listener: TcpListener,
    claim: TaskClaim,
    document: Vec<u8>,
    document_sha: flori_core::Sha256Digest,
    exec_id: AttemptId,
    upload_id: UploadId,
) -> thread::JoinHandle<AiAudit> {
    thread::spawn(move || {
        let mut uploaded = Vec::new();
        let mut entry = None;
        for step in 0..7 {
            let (mut stream, _) = listener.accept().expect("accept");
            let (head, body) = read_request(&mut stream);
            match step {
                0 => json_response(&mut stream, &claim),
                1 => content_response(&mut stream, &document, &document_sha),
                2 => {
                    assert!(head.contains(&format!("/attempts/{exec_id}/usage")));
                    assert!(String::from_utf8_lossy(&body).contains(r#""state":"started""#));
                    json_response(
                        &mut stream,
                        &UsageAck {
                            usage_id: AiUsageId::generate(),
                            state: AiUsageState::Started,
                            applied: false,
                        },
                    );
                }
                3 => {
                    let start: StartUploadRequest =
                        serde_json::from_slice(&body).expect("start upload");
                    let artifact = ArtifactManifestEntry {
                        name: start.name,
                        kind: ArtifactKind::AiAudit,
                        media_type: start.media_type,
                        size_bytes: start.size_bytes,
                        sha256: start.sha256,
                        relative_path: "sources/job/audit.json".into(),
                    };
                    entry = Some(artifact.clone());
                    json_response(
                        &mut stream,
                        &StartUploadResponse {
                            upload_id,
                            received_bytes: 0,
                            artifact,
                        },
                    );
                }
                4 => {
                    uploaded = body;
                    json_response(
                        &mut stream,
                        &UploadCursor {
                            upload_id,
                            received_bytes: uploaded.len() as u64,
                        },
                    );
                }
                5 => json_response(
                    &mut stream,
                    &VerifyUploadResponse {
                        upload_id,
                        artifact: entry.clone().expect("entry"),
                    },
                ),
                6 => {
                    assert!(String::from_utf8_lossy(&body).contains("usage_conflict"));
                    json_response(
                        &mut stream,
                        &AttemptAck {
                            exec_id,
                            state: AttemptState::Failed,
                        },
                    );
                }
                _ => unreachable!(),
            }
        }
        serde_json::from_slice(&uploaded).expect("strict audit")
    })
}
