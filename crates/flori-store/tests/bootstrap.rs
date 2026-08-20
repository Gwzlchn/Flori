use std::{fs, path::PathBuf};

use flori_store::Store;
use sqlx::Row;

const PIPELINE: &str = include_str!("../../../pipelines/pdf.yml");

#[tokio::test]
async fn pdf_bootstrap_is_idempotent_and_preserves_edited_prompts() {
    let root = temporary_root();
    fs::create_dir(&root).expect("test root");
    let database = root.join("flori.sqlite");
    let store = Store::open(&database).await.expect("empty store");
    let first = store
        .bootstrap_pdf(PIPELINE, "note-v1", "translate-v1", "test", 1)
        .await
        .expect("first bootstrap");

    let pool = sqlx::SqlitePool::connect(&format!("sqlite://{}", database.display()))
        .await
        .expect("inspect store");
    sqlx::query(
        "UPDATE prompts SET content='user-edited', \
         sha256='0cb4fd16e02844d2526859bd18f2e2b82f8139769ffece5e1f3995cc0e0ba0c5' \
         WHERE key='document_note'",
    )
    .execute(&pool)
    .await
    .expect("simulate future Prompt UI edit");
    pool.close().await;

    let second = store
        .bootstrap_pdf(PIPELINE, "note-v2", "translate-v2", "test", 2)
        .await
        .expect("repeat bootstrap");
    assert_eq!(second, first);
    assert_eq!(store.pdf_setup().await.expect("setup"), Some(first));

    let pool = sqlx::SqlitePool::connect(&format!("sqlite://{}", database.display()))
        .await
        .expect("inspect store");
    let prompt = sqlx::query("SELECT content FROM prompts WHERE key='document_note'")
        .fetch_one(&pool)
        .await
        .expect("prompt");
    assert_eq!(
        prompt.try_get::<String, _>("content").expect("content"),
        "user-edited"
    );
    let counts: (i64, i64, i64, i64) = sqlx::query_as(
        "SELECT (SELECT count(*) FROM domains),(SELECT count(*) FROM prompts), \
         (SELECT count(*) FROM pipelines),(SELECT count(*) FROM pipeline_revisions)",
    )
    .fetch_one(&pool)
    .await
    .expect("counts");
    assert_eq!(counts, (1, 2, 1, 1));
    pool.close().await;
    drop(store);
    fs::remove_dir_all(root).expect("remove fixture");
}

fn temporary_root() -> PathBuf {
    std::env::temp_dir().join(format!(
        "flori-bootstrap-{}",
        flori_core::RequestId::generate()
    ))
}
