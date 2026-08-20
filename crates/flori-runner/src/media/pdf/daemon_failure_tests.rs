use super::*;

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
