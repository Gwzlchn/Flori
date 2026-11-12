use std::collections::BTreeMap;

use super::*;
use crate::{exec::tests::config, pipeline::parse_source};

async fn pool(dir: &Path) -> SqlitePool {
    open(&dir.join("t.sqlite")).await.unwrap()
}

#[tokio::test]
async fn migrations_apply_and_step_names_match() {
    let dir = tempfile::tempdir().unwrap();
    let pool = pool(dir.path()).await;
    let tables: Vec<(String,)> =
        sqlx::query_as("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' AND name != 'sqlite_sequence' ORDER BY name")
            .fetch_all(&pool)
            .await
            .unwrap();
    let tables: Vec<&str> = tables.iter().map(|t| t.0.as_str()).collect();
    assert_eq!(
        tables,
        ["collections", "domains", "jobs", "prompts", "steps"]
    );

    async fn ddl(pool: &SqlitePool, table: &str) -> String {
        let sql = "SELECT sql FROM sqlite_master WHERE name = ?";
        let ddl: (String,) = sqlx::query_as(sql)
            .bind(table)
            .fetch_one(pool)
            .await
            .unwrap();
        ddl.0
    }
    let steps = ddl(&pool, "steps").await;
    for name in StepName::ALL {
        assert!(
            steps.contains(&format!("'{name}'")),
            "steps.name CHECK misses {name}"
        );
    }
    let prompts = ddl(&pool, "prompts").await;
    for name in PromptName::ALL {
        assert!(
            prompts.contains(&format!("'{name}'")),
            "prompts.name CHECK misses {name}"
        );
    }
}

#[tokio::test]
async fn prompts_are_one_per_name_and_scope_and_go_with_their_domain() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let ml = create_domain(&db, "ml").await.unwrap().unwrap().id;
    let arch = create_domain(&db, "计算机系统结构")
        .await
        .unwrap()
        .unwrap()
        .id;
    let set = |name, domain, text| set_prompt(&db, name, domain, text);
    set(PromptName::Translate, None, Some("a"))
        .await
        .unwrap()
        .unwrap();
    set(PromptName::Translate, None, Some("b"))
        .await
        .unwrap()
        .unwrap();
    set(PromptName::Translate, Some(ml), Some("ml"))
        .await
        .unwrap()
        .unwrap();
    set(PromptName::Translate, Some(arch), Some("x"))
        .await
        .unwrap()
        .unwrap();
    set(PromptName::Translate, Some(arch), Some("arch"))
        .await
        .unwrap()
        .unwrap();
    set(PromptName::NoteVideo, Some(arch), Some("v"))
        .await
        .unwrap()
        .unwrap();
    let missing = set(PromptName::Translate, Some(999), Some("z"))
        .await
        .unwrap();
    assert_eq!(missing.unwrap_err().code, ErrorCode::NotFound);

    let stored = prompts(&db, Some(arch)).await.unwrap();
    assert_eq!(
        stored.overrides,
        BTreeMap::from([(PromptName::Translate, "b".to_owned())])
    );
    assert_eq!(
        stored.domain,
        BTreeMap::from([
            (PromptName::NoteVideo, "v".to_owned()),
            (PromptName::Translate, "arch".to_owned()),
        ])
    );
    assert!(prompts(&db, None).await.unwrap().domain.is_empty());

    set(PromptName::Translate, None, None)
        .await
        .unwrap()
        .unwrap();
    let stored = prompts(&db, Some(ml)).await.unwrap();
    assert!(stored.overrides.is_empty());
    assert_eq!(stored.domain[&PromptName::Translate], "ml");

    // Requirements go with their domain; the domain had no collection or job to keep it.
    delete_domain(&db, arch).await.unwrap().unwrap();
    let left: Vec<(Option<i64>, String)> =
        sqlx::query_as("SELECT domain_id, text FROM prompts ORDER BY text")
            .fetch_all(&db)
            .await
            .unwrap();
    assert_eq!(left, [(Some(ml), "ml".to_owned())]);
}

#[test]
fn timestamps_are_whole_seconds_in_utc_plus_8() {
    let now = now();
    assert_eq!(now.len(), "2026-11-08T10:00:00+08:00".len(), "{now}");
    assert!(now.ends_with("+08:00"), "{now}");
}

#[tokio::test]
async fn jobs_are_created_once_per_source_with_steps_and_job_json() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let config = config(dir.path());
    let domain = create_domain(&db, "ml").await.unwrap().unwrap();
    let paper = parse_source("https://arxiv.org/abs/2401.12345v2").unwrap();
    let again = parse_source("2401.12345v1").unwrap();

    let first = create_jobs(
        &db,
        &config,
        "9.9.9",
        domain.id,
        None,
        TranslateMode::Off,
        &[paper.clone(), again],
    )
    .await
    .unwrap()
    .unwrap();
    let new = first[0].clone().unwrap();
    assert_eq!(first[1], Err(new.clone()), "v1 and v2 are the same paper");

    let names: Vec<StepName> = job_steps(&db, new.id)
        .await
        .unwrap()
        .into_iter()
        .map(|s| s.1)
        .collect();
    assert_eq!(
        names,
        [
            StepName::ArxivFetch,
            StepName::ArxivConvert,
            StepName::Note,
            StepName::Publish
        ]
    );
    let spec = crate::pipeline::read_job(&job_dir(&config.data_dir, new.id)).unwrap();
    assert_eq!(
        (spec.job_id, spec.domain.as_str(), spec.collection),
        (new.id, "ml", None)
    );
    assert_eq!(spec.ai.cli_version, "9.9.9");
    assert_eq!(spec.source, paper);
    assert!(spec.created.ends_with("+08:00"));

    // Same source in a collection of the same domain is a different job.
    let coll = create_collection(&db, domain.id, "papers")
        .await
        .unwrap()
        .unwrap();
    let other = create_jobs(
        &db,
        &config,
        "9.9.9",
        domain.id,
        Some(coll.id),
        TranslateMode::Auto,
        std::slice::from_ref(&paper),
    )
    .await
    .unwrap()
    .unwrap();
    assert_ne!(other[0].clone().unwrap().id, new.id);

    let missing = create_jobs(
        &db,
        &config,
        "x",
        domain.id,
        Some(999),
        TranslateMode::Auto,
        &[paper],
    )
    .await
    .unwrap();
    assert_eq!(missing.unwrap_err().code, ErrorCode::NotFound);
}

#[tokio::test]
async fn failed_job_json_write_rolls_back() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let mut config = config(dir.path());
    // A file where the jobs directory should be makes every job.json write fail.
    fs::write(dir.path().join("blocker"), "").unwrap();
    config.data_dir = dir.path().join("blocker");
    let domain = create_domain(&db, "ml").await.unwrap().unwrap();
    let source = parse_source("2401.12345").unwrap();
    let err = create_jobs(
        &db,
        &config,
        "x",
        domain.id,
        None,
        TranslateMode::Auto,
        &[source],
    )
    .await;
    assert!(matches!(err, Err(sqlx::Error::Io(_))), "{err:?}");
    assert!(list_jobs(&db, 10).await.unwrap().is_empty());
    let steps: i64 = sqlx::query_scalar("SELECT count(*) FROM steps")
        .fetch_one(&db)
        .await
        .unwrap();
    assert_eq!(steps, 0);
}

#[tokio::test]
async fn executor_writes_never_override_cancel() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let id = queued_job(&db, dir.path()).await;
    assert!(claim_job(&db, id).await.unwrap());
    assert_eq!(start_attempt(&db, id, 1).await.unwrap(), Some(1));
    cancel_job(&db, id).await.unwrap().unwrap();

    assert!(
        !end_attempt(&db, id, 1, StepStatus::Failed, Some(20), Some("late"))
            .await
            .unwrap()
    );
    end_job(&db, id, JobStatus::Failed).await.unwrap();
    assert_eq!(start_attempt(&db, id, 2).await.unwrap(), None);
    let (summary, _) = get_job(&db, id).await.unwrap().unwrap();
    assert_eq!(summary.status, JobStatus::Canceled);
    assert_eq!(summary.current_step, Some(StepName::ArxivFetch));
    assert_eq!(
        steps(&db, id).await.unwrap()[0].status,
        StepStatus::Canceled
    );

    // retry resumes the canceled step and keeps its attempt count.
    let retried = retry_job(&db, id).await.unwrap().unwrap();
    assert_eq!(
        (retried.status, retried.current_step),
        (JobStatus::Queued, None)
    );
    assert!(claim_job(&db, id).await.unwrap());
    assert_eq!(start_attempt(&db, id, 1).await.unwrap(), Some(2));
    assert_eq!(
        retry_job(&db, id).await.unwrap().unwrap_err().code,
        ErrorCode::Conflict
    );
    assert_eq!(
        cancel_job(&db, 999).await.unwrap().unwrap_err().code,
        ErrorCode::NotFound
    );
}

#[tokio::test]
async fn job_rows_carry_the_current_step_exit_code() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let id = queued_job(&db, dir.path()).await;
    let exit_code = || async {
        let (job, _) = get_job(&db, id).await.unwrap().unwrap();
        (job.current_step, job.current_exit_code)
    };
    assert_eq!(exit_code().await, (None, None));
    claim_job(&db, id).await.unwrap();
    start_attempt(&db, id, 1).await.unwrap();
    end_attempt(&db, id, 1, StepStatus::Succeeded, Some(0), None)
        .await
        .unwrap();
    start_attempt(&db, id, 2).await.unwrap();
    end_attempt(&db, id, 2, StepStatus::Failed, Some(30), Some("login"))
        .await
        .unwrap();
    assert_eq!(exit_code().await, (Some(StepName::ArxivConvert), Some(30)));
    let listed = &list_jobs(&db, 10).await.unwrap()[0];
    assert_eq!(listed.current_step, Some(StepName::ArxivConvert));
    assert_eq!(listed.current_exit_code, Some(30));
}

async fn queued_job(db: &SqlitePool, dir: &Path) -> i64 {
    let config = config(dir);
    let domain = create_domain(db, "ml").await.unwrap().unwrap();
    let source = parse_source("2401.12345").unwrap();
    create_jobs(
        db,
        &config,
        "x",
        domain.id,
        None,
        TranslateMode::Off,
        &[source],
    )
    .await
    .unwrap()
    .unwrap()[0]
        .clone()
        .unwrap()
        .id
}

#[tokio::test]
async fn step_success_and_job_output_commit_together() {
    let dir = tempfile::tempdir().unwrap();
    let db = pool(dir.path()).await;
    let id = queued_job(&db, dir.path()).await;
    claim_job(&db, id).await.unwrap();
    start_attempt(&db, id, 1).await.unwrap();
    sqlx::raw_sql(
        "CREATE TRIGGER no_title BEFORE UPDATE OF title ON jobs \
         BEGIN SELECT RAISE(ABORT, 'disk full'); END;",
    )
    .execute(&db)
    .await
    .unwrap();
    assert!(
        step_succeeded(&db, id, 1, Some("Paper"), None)
            .await
            .is_err()
    );
    assert_eq!(
        steps(&db, id).await.unwrap()[0].status,
        StepStatus::Running,
        "rolled back"
    );

    sqlx::raw_sql("DROP TRIGGER no_title")
        .execute(&db)
        .await
        .unwrap();
    assert!(
        step_succeeded(&db, id, 1, Some("Paper"), None)
            .await
            .unwrap()
    );
    let (job, _) = get_job(&db, id).await.unwrap().unwrap();
    assert_eq!(
        (job.author, job.published, job.duration_ms, job.description),
        (None, None, None, None),
        "nothing before publish"
    );
    start_attempt(&db, id, 4).await.unwrap();
    let note = Published {
        vault_path: "flori/ml/x/".into(),
        author: Some("A. Author, B. Author".into()),
        published: Some("2024-01-08".into()),
        duration_ms: None,
        description: Some("一句话。".into()),
    };
    assert!(
        step_succeeded(&db, id, 4, Some("Later"), Some(&note))
            .await
            .unwrap()
    );
    let (job, vault_path) = get_job(&db, id).await.unwrap().unwrap();
    assert_eq!(
        (job.title.as_deref(), vault_path.as_deref()),
        (Some("Paper"), Some("flori/ml/x/"))
    );
    assert_eq!(
        (job.author, job.published, job.duration_ms, job.description),
        (note.author, note.published, None, note.description)
    );
    assert!(
        !step_succeeded(&db, id, 4, None, None).await.unwrap(),
        "not running any more"
    );
}

/// With one connection, a refusal that asked the pool for a second connection while holding
/// the transaction would wait for the acquire timeout.
#[tokio::test]
async fn refusals_answer_on_the_held_connection() {
    let dir = tempfile::tempdir().unwrap();
    let options = SqliteConnectOptions::new()
        .filename(dir.path().join("one.sqlite"))
        .create_if_missing(true)
        .foreign_keys(true);
    let db = SqlitePoolOptions::new()
        .max_connections(1)
        .acquire_timeout(Duration::from_secs(10))
        .connect_with(options)
        .await
        .unwrap();
    sqlx::migrate!("./migrations").run(&db).await.unwrap();
    let id = queued_job(&db, dir.path()).await;
    sqlx::query("UPDATE jobs SET status = 'succeeded' WHERE id = ?")
        .bind(id)
        .execute(&db)
        .await
        .unwrap();
    let quick = Duration::from_secs(3);
    let cancel = tokio::time::timeout(quick, cancel_job(&db, id)).await;
    assert_eq!(
        cancel.unwrap().unwrap().unwrap_err().code,
        ErrorCode::Conflict
    );
    let retry = tokio::time::timeout(quick, retry_job(&db, id)).await;
    assert_eq!(
        retry.unwrap().unwrap().unwrap_err().code,
        ErrorCode::Conflict
    );
    let missing = tokio::time::timeout(quick, cancel_job(&db, id + 1)).await;
    assert_eq!(
        missing.unwrap().unwrap().unwrap_err().code,
        ErrorCode::NotFound
    );
}
