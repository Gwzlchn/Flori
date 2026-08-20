use super::*;

#[tokio::test]
async fn codex_daemon_publishes_over_real_http_sqlite_and_nas() {
    let (harness, job_id) = Harness::new().await;
    for expected in ["acquire", "extract"] {
        let claim = harness.client.poll().await.expect("poll").expect("claim");
        assert_eq!((claim.job_id, claim.task_key.as_str()), (job_id, expected));
        if expected == "acquire" {
            assert_source_content(&harness, &claim).await;
        } else {
            assert_artifact_content(&harness, &claim).await;
        }
        run_runner_task(&harness.client, &claim).await;
    }

    let original_id: ArtifactId = sqlx::query_scalar::<_, String>(
        "SELECT a.id FROM artifacts a JOIN tasks t ON t.id=a.task_id \
         WHERE a.job_id=? AND t.task_key='acquire' AND a.kind='source_original'",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("source artifact")
    .parse()
    .expect("typed artifact ID");
    let evidence_id = EvidenceId::generate();
    let smart_note = serde_json::to_string(&format!(
        "# Smart note\n\n## 来源事实\nAttention is all you need. [[evidence:{evidence_id}]]\n\n## AI 分析\nThe source motivates attention.\n"
    ))
    .expect("smart note JSON string");
    let summary = serde_json::to_string(&format!(
        "Attention is all you need. [[evidence:{evidence_id}]]"
    ))
    .expect("summary JSON string");
    let envelope = format!(
        r#"{{"executor":"ai.document_note","schema":"flori.ai_result.v1","smart_note_markdown":{smart_note},"summary_markdown":{summary},"terms":{{"schema":"flori.terms.v1","terms":[{{"term":"Attention","explanation":"A mechanism that relates positions.","evidence_ids":["{evidence_id}"]}}],"evidence_candidates":[{{"evidence_id":"{evidence_id}","source_artifact_id":"{original_id}","locator":{{"kind":"pdf","value":{{"page":1,"bbox":{{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0}}}}}},"quote":"Attention is all you need."}}]}}}}"#,
    );
    let agent = format!(
        r#"{{"type":"item.completed","item":{{"id":"item","type":"agent_message","text":{}}}}}"#,
        serde_json::to_string(&envelope).expect("nested result")
    );
    let events = [
        r#"{"type":"thread.started","thread_id":"thread"}"#.to_owned(),
        r#"{"type":"turn.started"}"#.to_owned(),
        agent,
        r#"{"type":"turn.completed","usage":{"input_tokens":41,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":17,"reasoning_output_tokens":1}}"#.to_owned(),
    ];
    let executable = harness.root.join("fake-codex");
    let captured_argv = harness.root.join("fake-codex.argv");
    let captured_stdin = harness.root.join("fake-codex.stdin");
    let event_writes = events
        .iter()
        .map(|event| format!("printf '%s\\n' '{event}'"))
        .collect::<Vec<_>>()
        .join("\n");
    let script = format!(
        "#!/bin/sh\nresult=''\nprintf '%s\\n' \"$@\" > '{argv}'\n\
         while [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--output-last-message' ]; then shift; result=$1; fi\n  shift\ndone\n\
         cat > '{stdin}'\nprintf '%s' '{envelope}' > \"$result\"\n{event_writes}\n",
        argv = captured_argv.display(),
        stdin = captured_stdin.display(),
        envelope = envelope,
    );
    fs::write(&executable, script).expect("fake Codex");
    let mut permissions = fs::metadata(&executable)
        .expect("fake metadata")
        .permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(&executable, permissions).expect("fake executable");
    for directory in ["daemon-home", "daemon-config", "daemon-work"] {
        fs::create_dir(harness.root.join(directory)).expect("daemon directory");
    }
    let config = DaemonConfig {
        tool: AiTool::CodexCli,
        executable,
        home: harness.root.join("daemon-home"),
        tool_config_home: harness.root.join("daemon-config"),
        work_root: harness.root.join("daemon-work"),
        model: "model-a".into(),
        effort: "high".into(),
        renew_interval: Duration::from_millis(100),
        max_output_bytes: 1024 * 1024,
        proxy_url: Some(
            "http://codex-proxy.test:10810"
                .parse()
                .expect("test Codex proxy URL"),
        ),
    };
    let daemon_client = RunnerClient::new(
        &format!("http://{}", harness.address),
        harness.runner_token.clone(),
    )
    .expect("daemon client");
    let (cancel_tx, mut cancel_rx) = watch::channel(false);
    let daemon =
        tokio::spawn(async move { run_ai_daemon(&daemon_client, &config, &mut cancel_rx).await });
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let state: String =
                sqlx::query_scalar("SELECT state FROM tasks WHERE job_id=? AND task_key='note'")
                    .bind(job_id.to_string())
                    .fetch_one(&harness.pool)
                    .await
                    .expect("note state");
            if state == "succeeded" {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("AI daemon completion");
    cancel_tx.send(true).expect("cancel daemon");
    assert_eq!(daemon.await.expect("daemon join"), Ok(()));

    assert!(harness.client.poll().await.expect("drive core").is_none());
    assert_published(&harness, job_id, 1).await;
    let pointers: (Option<String>, Option<String>) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("publication pointers");
    assert_eq!(pointers, (Some(job_id.to_string()), None));

    let usage: (i64, String, String, Option<i64>, Option<i64>, Option<i64>) = sqlx::query_as(
        "SELECT count(*),min(state),min(tool),min(input_tokens),min(output_tokens),min(credits_micros) FROM ai_usage WHERE job_id=?",
    )
    .bind(job_id.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("Codex usage");
    assert_eq!(
        usage,
        (
            1,
            "final".into(),
            "codex_cli".into(),
            Some(41),
            Some(17),
            None
        )
    );

    let note_artifacts: BTreeMap<String, String> = sqlx::query_as(
        "SELECT a.name,a.relative_path FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? AND t.task_key='note'",
    )
    .bind(job_id.to_string())
    .fetch_all(&harness.pool)
    .await
    .expect("note artifacts")
    .into_iter()
    .collect();
    let artifact = |name: &str| {
        fs::read(harness.root.join("artifacts").join(&note_artifacts[name])).expect("NAS artifact")
    };
    assert!(
        String::from_utf8(artifact("smart_note"))
            .expect("note UTF-8")
            .contains("## AI 分析")
    );
    assert!(
        String::from_utf8(artifact("summary"))
            .expect("summary UTF-8")
            .contains("[[evidence:")
    );
    let terms: flori_core::TermsManifest =
        serde_json::from_slice(&artifact("terms")).expect("strict terms");
    assert_eq!((terms.terms.len(), terms.evidence_candidates.len()), (1, 1));
    let audit: AiAudit = serde_json::from_slice(&artifact("audit")).expect("strict AI audit");
    assert_eq!(
        (audit.tool, audit.model.as_str()),
        (AiTool::CodexCli, "model-a")
    );
    assert_eq!(audit.effort, "high");
    assert_eq!(audit.usage_invocation_keys, ["primary"]);
    assert_eq!((audit.exit_code, audit.timed_out), (Some(0), false));
    assert!(audit.websearch_enabled);
    assert!(audit.websearch_urls.is_empty());

    let argv = fs::read_to_string(captured_argv).expect("captured argv");
    let stdin = fs::read_to_string(captured_stdin).expect("captured stdin");
    let log = String::from_utf8(artifact("log")).expect("task log");
    assert!(stdin.contains("PROMPT 4\nnote\n"));
    assert!(stdin.contains(r#"{"schema":"flori.document_structure.v1"#));
    assert!(!argv.contains("flori.document_structure.v1"));
    assert!(!argv.contains(&stdin));
    assert!(log.contains("AI task started"));
    assert!(!log.contains("flori.document_structure.v1"));
    assert!(!log.contains(&stdin));
    assert!(
        [&argv, &stdin, &log]
            .into_iter()
            .all(|surface| !surface.contains(&harness.runner_token))
    );
    assert!(
        audit
            .redacted_arguments
            .iter()
            .all(|argument| !argument.contains("flori.document_structure.v1")
                && !argument.contains(&harness.runner_token))
    );
    harness.close().await;
}
