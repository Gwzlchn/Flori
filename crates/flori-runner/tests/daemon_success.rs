#[path = "support/daemon.rs"]
mod support;

use std::{collections::BTreeMap, fs, net::TcpListener, thread, time::Duration};

use flori_core::{
    AiTool, AiUsageId, AiUsageState, ArtifactKind, ArtifactManifestEntry, ArtifactWhen, AttemptAck,
    AttemptId, AttemptState, Executor, ResolvedTaskInputs, StartUploadRequest, StartUploadResponse,
    TaskClaim, UploadCursor, UploadId, UsageAck, VerifyUploadResponse,
};
use tokio::sync::watch;

use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use support::*;

#[tokio::test]
async fn qoder_translation_completes_with_declared_outputs_and_framed_prompt() {
    let root = temp_root("success");
    let captured = root.join("captured-prompt");
    let result = r#"{"executor":"ai.document_translate","schema":"flori.ai_result.v1","translation_markdown":"translated"}"#;
    let outer = format!(
        r#"{{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":{},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":1.25,"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake","session_id":"fake"}}"#,
        serde_json::to_string(result).expect("nested result")
    );
    let executable = script(
        &root,
        "qoder-success",
        &format!("cat > '{}'\nprintf '%s' '{outer}'\n", captured.display()),
    );
    let document = br#"{"schema":"flori.document.v1","sections":[]}"#.to_vec();
    let exec_id = AttemptId::generate();
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base_url = format!("http://{}", listener.local_addr().expect("address"));
    let claim = claim(
        exec_id,
        Executor::AiDocumentTranslate,
        ResolvedTaskInputs::AiDocumentTranslate {
            document: artifact(&base_url, &document),
            prompt: prompt("translate faithfully"),
            profile: None,
        },
        vec![
            declaration(
                "translation",
                ArtifactKind::Translation,
                ArtifactWhen::OnSuccess,
            ),
            declaration("audit", ArtifactKind::AiAudit, ArtifactWhen::Always),
        ],
        5_000,
    );
    let client = RunnerClient::new(&base_url, "token").expect("client");
    let config = config(&root, executable, Duration::from_secs(1));
    let (stop, mut cancel) = watch::channel(false);
    let server = server(
        listener,
        claim,
        document.clone(),
        digest(&document),
        exec_id,
        stop,
    );
    run_ai_daemon(&client, &config, &mut cancel)
        .await
        .expect("daemon exits after success");
    let uploaded = server.join().expect("server");
    assert_eq!(
        uploaded.get("translation").map(Vec::as_slice),
        Some(&b"translated"[..])
    );
    let audit: flori_core::AiAudit =
        serde_json::from_slice(uploaded.get("audit").expect("audit")).expect("strict audit");
    assert_eq!(audit.tool, AiTool::QoderCli);
    assert_eq!(audit.usage_invocation_keys, ["primary"]);
    let prompt = fs::read_to_string(captured).expect("captured prompt");
    let document_section = format!("DOCUMENT {}\n", document.len());
    for section in [
        "EXECUTOR 21\nai.document_translate\n",
        "PROMPT SNAPSHOT SHA256 64\n",
        "PROMPT 20\ntranslate faithfully\n",
        "DOCUMENT NAME 9\nstructure\n",
        "DOCUMENT MEDIA TYPE 16\napplication/json\n",
        "DOCUMENT SHA256 64\n",
        &document_section,
        "AI RESULT JSON SCHEMA ",
    ] {
        assert!(prompt.contains(section), "missing {section}");
    }
    fs::remove_dir_all(root).expect("cleanup");
}

fn server(
    listener: TcpListener,
    claim: TaskClaim,
    document: Vec<u8>,
    document_sha: flori_core::Sha256Digest,
    exec_id: AttemptId,
    stop: watch::Sender<bool>,
) -> thread::JoinHandle<BTreeMap<String, Vec<u8>>> {
    thread::spawn(move || {
        let mut uploaded = BTreeMap::new();
        let mut current = None;
        for step in 0..11 {
            let (mut stream, _) = listener.accept().expect("accept");
            let (head, body) = read_request(&mut stream);
            match step {
                0 => json_response(&mut stream, &claim),
                1 => content_response(&mut stream, &document, &document_sha),
                2 | 3 => json_response(
                    &mut stream,
                    &UsageAck {
                        usage_id: AiUsageId::generate(),
                        state: if step == 2 {
                            AiUsageState::Started
                        } else {
                            AiUsageState::Final
                        },
                        applied: true,
                    },
                ),
                4 | 7 => {
                    let request: StartUploadRequest = serde_json::from_slice(&body).expect("start");
                    let upload_id = UploadId::generate();
                    let artifact = ArtifactManifestEntry {
                        kind: if request.name == "translation" {
                            ArtifactKind::Translation
                        } else {
                            ArtifactKind::AiAudit
                        },
                        name: request.name.clone(),
                        media_type: request.media_type,
                        size_bytes: request.size_bytes,
                        sha256: request.sha256,
                        relative_path: format!("sources/job/{}", request.name),
                    };
                    current = Some((upload_id, request.name, artifact.clone()));
                    json_response(
                        &mut stream,
                        &StartUploadResponse {
                            upload_id,
                            received_bytes: 0,
                            artifact,
                        },
                    );
                }
                5 | 8 => {
                    let (upload_id, name, _) = current.as_ref().expect("upload");
                    uploaded.insert(name.clone(), body.clone());
                    json_response(
                        &mut stream,
                        &UploadCursor {
                            upload_id: *upload_id,
                            received_bytes: body.len() as u64,
                        },
                    );
                }
                6 | 9 => {
                    let (upload_id, _, artifact) = current.take().expect("upload");
                    json_response(
                        &mut stream,
                        &VerifyUploadResponse {
                            upload_id,
                            artifact,
                        },
                    );
                }
                10 => {
                    assert!(head.contains(&format!("/attempts/{exec_id}/complete")));
                    assert!(String::from_utf8_lossy(&body).contains("manifest_sha256"));
                    json_response(
                        &mut stream,
                        &AttemptAck {
                            exec_id,
                            state: AttemptState::Succeeded,
                        },
                    );
                    thread::sleep(Duration::from_millis(50));
                    stop.send(true).expect("stop");
                }
                _ => unreachable!(),
            }
        }
        uploaded
    })
}
