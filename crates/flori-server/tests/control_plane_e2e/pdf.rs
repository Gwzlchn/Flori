use super::*;

#[tokio::test]
async fn pdf_control_plane_executes_and_reruns_over_real_http_sqlite_and_nas() {
    let (harness, first) = Harness::new().await;
    let first_ids = task_ids(&harness.pool, first).await;
    execute_and_publish(&harness, first).await;
    assert_published(&harness, first, 1).await;

    let pipeline = RerunJobRequest {
        request_key: "pipeline-rerun".into(),
        mode: RerunMode::Pipeline,
        from_task_key: None,
        ai_selection: None,
    };
    let second = rerun_http(&harness, first, &pipeline).await;
    assert_eq!(rerun_http(&harness, first, &pipeline).await, second);
    let second_ids = task_ids(&harness.pool, second).await;
    assert!(
        first_ids
            .values()
            .all(|id| !second_ids.values().any(|new| new == id))
    );
    execute_and_publish(&harness, second).await;
    assert_published(&harness, second, 2).await;
    let pointers: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("publication pointers");
    assert_eq!(pointers, (second.to_string(), first.to_string()));

    let translate = rerun_http(
        &harness,
        second,
        &RerunJobRequest {
            request_key: "translate-rerun".into(),
            mode: RerunMode::FromTask,
            from_task_key: Some("translate".into()),
            ai_selection: None,
        },
    )
    .await;
    let translated_inputs: String = sqlx::query_scalar("SELECT inputs_json FROM jobs WHERE id=?")
        .bind(translate.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("translation inputs");
    assert_eq!(translated_inputs, r#"{"translate":true}"#);
    let claim = harness
        .client
        .poll()
        .await
        .expect("poll translate")
        .expect("translate claim");
    assert_eq!(
        (claim.job_id, claim.task_key.as_str()),
        (translate, "translate")
    );
    fail_runner_task(&harness.client, &claim, ErrorCode::ExecutorFailed).await;
    let failed: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
        .bind(translate.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("failed translation Job");
    assert_eq!(failed, "failed");
    let unchanged: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("unchanged publication pointers");
    assert_eq!(unchanged, (second.to_string(), first.to_string()));

    sqlx::query("UPDATE sources SET current_job_id=?,previous_job_id=NULL WHERE id=?")
        .bind(first.to_string())
        .bind(harness.source_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("drift current");
    let drift = post_json(
        &harness,
        &format!("/api/v1/jobs/{second}/rerun"),
        &serde_json::to_string(&RerunJobRequest {
            request_key: "current-drift".into(),
            mode: RerunMode::FromTask,
            from_task_key: Some("note".into()),
            ai_selection: None,
        })
        .expect("rerun JSON"),
    )
    .await;
    assert_error(&drift, 409, ErrorCode::RerunBoundaryInvalid);
    sqlx::query("UPDATE sources SET current_job_id=?,previous_job_id=? WHERE id=?")
        .bind(second.to_string())
        .bind(first.to_string())
        .bind(harness.source_id.to_string())
        .execute(&harness.pool)
        .await
        .expect("restore current");

    let translated = rerun_http(
        &harness,
        second,
        &RerunJobRequest {
            request_key: "translate-success".into(),
            mode: RerunMode::FromTask,
            from_task_key: Some("translate".into()),
            ai_selection: None,
        },
    )
    .await;
    assert_ne!(translated, translate);
    let claim = harness
        .client
        .poll()
        .await
        .expect("poll successful translation")
        .expect("translation claim");
    assert_eq!(
        (claim.job_id, claim.task_key.as_str()),
        (translated, "translate")
    );
    run_runner_task(&harness.client, &claim).await;
    assert!(harness.client.poll().await.expect("drive core").is_none());
    let translated_state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
        .bind(translated.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("translated Job state");
    assert_eq!(translated_state, "succeeded");
    let translated_artifact: (String, String) = sqlx::query_as(
        "SELECT origin,relative_path FROM artifacts WHERE job_id=? AND kind='translation'",
    )
    .bind(translated.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("translation artifact");
    assert_eq!(translated_artifact.0, "produced");
    assert_eq!(
        fs::read(harness.root.join("artifacts").join(translated_artifact.1))
            .expect("translation bytes"),
        b"# Translation\n\nAttention is all you need.\n"
    );
    assert_pdf_artifact_ids(&harness, translated).await;
    let pointers: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("translated publication pointers");
    assert_eq!(pointers, (translated.to_string(), second.to_string()));

    let runner_revision: i64 = sqlx::query_scalar("SELECT config_revision FROM runners WHERE id=?")
        .bind(harness.other_runner_id.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("other Runner revision");
    let from_note = rerun_http(
        &harness,
        translated,
        &RerunJobRequest {
            request_key: "note-rerun".into(),
            mode: RerunMode::FromTask,
            from_task_key: Some("note".into()),
            ai_selection: Some(AiRunnerSelection {
                task_key: "note".into(),
                runner_id: harness.other_runner_id,
                model: "model-a".into(),
                effort: "high".into(),
                runner_config_revision: runner_revision
                    .try_into()
                    .expect("nonnegative Runner revision"),
            }),
        },
    )
    .await;
    let states: BTreeMap<String, String> =
        sqlx::query_as("SELECT task_key,state FROM tasks WHERE job_id=?")
            .bind(from_note.to_string())
            .fetch_all(&harness.pool)
            .await
            .expect("rerun states")
            .into_iter()
            .collect();
    assert_eq!(
        (&states["extract"], &states["note"], &states["validate"]),
        (&"skipped".into(), &"ready".into(), &"pending".into())
    );
    let materialized_translation: i64 = sqlx::query_scalar(
        "SELECT count(*) FROM artifacts WHERE job_id=? AND origin='materialized' AND kind='translation'",
    )
    .bind(from_note.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("materialized translation");
    assert_eq!(materialized_translation, 1);
    let selected: (String, String, String, i64) = sqlx::query_as(
        "SELECT pinned_runner_id,selected_model,selected_effort,runner_config_revision \
         FROM tasks WHERE job_id=? AND task_key='note'",
    )
    .bind(from_note.to_string())
    .fetch_one(&harness.pool)
    .await
    .expect("selected AI Runner");
    assert_eq!(
        selected,
        (
            harness.other_runner_id.to_string(),
            "model-a".into(),
            "high".into(),
            runner_revision,
        )
    );
    assert!(
        harness
            .client
            .poll()
            .await
            .expect("default Runner poll")
            .is_none()
    );
    let pinned_client = RunnerClient::new(
        &format!("http://{}", harness.address),
        harness.other_runner_token.clone(),
    )
    .expect("pinned Runner client");
    let claim = pinned_client
        .poll()
        .await
        .expect("pinned Runner poll")
        .expect("pinned note claim");
    assert_eq!((claim.job_id, claim.task_key.as_str()), (from_note, "note"));
    assert_eq!(
        (
            claim.model.as_deref(),
            claim.effort.as_deref(),
            claim.runner_config_revision
        ),
        (Some("model-a"), Some("high"), runner_revision as u64)
    );
    run_runner_task(&pinned_client, &claim).await;
    assert!(pinned_client.poll().await.expect("drive core").is_none());
    let note_state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
        .bind(from_note.to_string())
        .fetch_one(&harness.pool)
        .await
        .expect("note rerun state");
    assert_eq!(note_state, "succeeded");
    assert_pdf_artifact_ids(&harness, from_note).await;
    let pointers: (String, String) =
        sqlx::query_as("SELECT current_job_id,previous_job_id FROM sources WHERE id=?")
            .bind(harness.source_id.to_string())
            .fetch_one(&harness.pool)
            .await
            .expect("note publication pointers");
    assert_eq!(pointers, (from_note.to_string(), translated.to_string()));

    let conflict = post_json(
        &harness,
        &format!("/api/v1/jobs/{first}/rerun"),
        r#"{"request_key":"pipeline-rerun","mode":"from_task","from_task_key":"note","ai_selection":null}"#,
    )
    .await;
    assert_error(&conflict, 409, ErrorCode::IdempotencyConflict);
    let unknown = post_json(
        &harness,
        &format!("/api/v1/jobs/{first}/rerun"),
        &format!(
            r#"{{"request_key":"unknown-field","mode":"pipeline","from_task_key":null,"ai_selection":null,"source_id":"{}"}}"#,
            harness.source_id
        ),
    )
    .await;
    assert_error(&unknown, 400, ErrorCode::InvalidRequest);
    let missing = post_json(
        &harness,
        &format!("/api/v1/jobs/{}/rerun", JobId::generate()),
        r#"{"request_key":"missing-job","mode":"pipeline","from_task_key":null,"ai_selection":null}"#,
    )
    .await;
    assert_error(&missing, 404, ErrorCode::NotFound);
    harness.close().await;
}
