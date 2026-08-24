#[path = "support/daemon.rs"]
mod support;

use std::{fs, net::TcpListener, thread, time::Duration};

use flori_core::{
    AiUsageId, AiUsageState, ArtifactKind, ArtifactWhen, AttemptId, ErrorCode, Executor,
    ResolvedTaskInputs, UsageAck, UsageUpdate,
};
use tokio::sync::watch;

use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use support::*;

#[tokio::test]
async fn local_lease_deadline_kills_cli_while_renew_is_stalled() {
    let root = temp_root("renew");
    let finished = root.join("finished");
    let executable = script(
        &root,
        "qoder-slow",
        &format!(
            "(sleep 0.65; touch '{}') &\ncat >/dev/null\nwhile :; do :; done\n",
            finished.display()
        ),
    );
    let document = b"document".to_vec();
    let exec_id = AttemptId::generate();
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base_url = format!("http://{}", listener.local_addr().expect("address"));
    let mut claim = claim(
        exec_id,
        Executor::AiDocumentTranslate,
        ResolvedTaskInputs::AiDocumentTranslate {
            document: artifact(&base_url, &document),
            prompt: prompt("translate"),
            profile: None,
        },
        vec![declaration(
            "audit",
            ArtifactKind::AiAudit,
            ArtifactWhen::Always,
        )],
        10_000,
    );
    claim.lease_expires_at_ms = now_ms() + 500;
    let client = RunnerClient::new(&base_url, "token").expect("client");
    let config = config(&root, executable, Duration::from_millis(30));
    let (_keep, mut cancel) = watch::channel(false);
    let server = server(listener, claim, document, digest(b"document"), exec_id);
    assert_eq!(
        run_ai_daemon(&client, &config, &mut cancel).await,
        Err(ErrorCode::LeaseExpired)
    );
    server.join().expect("server");
    assert!(!finished.exists());
    fs::remove_dir_all(root).expect("cleanup");
}

fn server(
    listener: TcpListener,
    claim: flori_core::TaskClaim,
    document: Vec<u8>,
    document_sha: flori_core::Sha256Digest,
    exec_id: AttemptId,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept poll");
        let (head, _) = read_request(&mut stream);
        assert!(head.starts_with("POST /runner/v1/poll "));
        json_response(&mut stream, &claim);

        for _ in 0..6 {
            let (mut stream, _) = listener.accept().expect("accept");
            let (head, body) = read_request(&mut stream);
            let request = head.lines().next().expect("request line");
            if request.starts_with("GET /api/v1/artifacts/") {
                content_response(&mut stream, &document, &document_sha);
            } else if request.contains("/usage ") {
                let update: UsageUpdate = serde_json::from_slice(&body).expect("usage");
                assert!(matches!(update, UsageUpdate::Started { .. }));
                json_response(
                    &mut stream,
                    &UsageAck {
                        usage_id: AiUsageId::generate(),
                        state: AiUsageState::Started,
                        applied: true,
                    },
                );
            } else if request.contains(&format!("/attempts/{exec_id}/renew ")) {
                thread::sleep(Duration::from_millis(800));
                return;
            } else {
                panic!("unexpected request: {request}");
            }
        }
        panic!("renew request was not observed");
    })
}
