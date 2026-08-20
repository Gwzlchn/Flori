#[path = "support/daemon.rs"]
mod support;

use std::{fs, net::TcpListener, thread, time::Duration};

use flori_core::{ArtifactKind, ArtifactWhen, AttemptId, ErrorCode, Executor, ResolvedTaskInputs};
use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use tokio::sync::watch;

use support::*;

#[tokio::test]
async fn mismatched_model_is_rejected_before_download_usage_or_spawn() {
    let root = temp_root("model-mismatch");
    let marker = root.join("spawned");
    let executable = script(
        &root,
        "fake-qoder",
        &format!("touch '{}'\n", marker.display()),
    );
    let document = b"document".to_vec();
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base_url = format!("http://{}", listener.local_addr().expect("address"));
    let mut claim = claim(
        AttemptId::generate(),
        Executor::AiDocumentNote,
        ResolvedTaskInputs::AiDocumentNote {
            document: artifact(&base_url, &document),
            prompt: prompt("note"),
            profile: None,
        },
        vec![declaration(
            "audit",
            ArtifactKind::AiAudit,
            ArtifactWhen::Always,
        )],
        2_000,
    );
    claim.model = Some("other-model".into());
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("poll");
        let (head, _) = read_request(&mut stream);
        assert!(head.starts_with("POST /runner/v1/poll "));
        json_response(&mut stream, &claim);
    });
    let client = RunnerClient::new(&base_url, "runner-token").expect("client");
    let config = config(&root, executable, Duration::from_secs(1));
    let (_keep, mut cancel) = watch::channel(false);
    assert_eq!(
        run_ai_daemon(&client, &config, &mut cancel).await,
        Err(ErrorCode::CorruptState)
    );
    server.join().expect("server");
    assert!(!marker.exists(), "mismatch must not spawn CLI");
    fs::remove_dir_all(root).expect("cleanup");
}
