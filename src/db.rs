//! SQLite with runtime queries (no query macros). Migrations are embedded and only move forward.

use std::{path::Path, time::Duration};

use sqlx::{
    SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous},
};

pub async fn open(path: &Path) -> Result<SqlitePool, sqlx::Error> {
    let options = SqliteConnectOptions::new()
        .filename(path)
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Normal)
        .foreign_keys(true)
        .busy_timeout(Duration::from_secs(5));
    let pool = SqlitePoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    Ok(pool)
}

#[cfg(test)]
mod tests {
    use crate::pipeline::StepName;

    #[tokio::test]
    async fn migrations_apply_and_step_names_match() {
        let dir = tempfile::tempdir().unwrap();
        let pool = super::open(&dir.path().join("t.sqlite")).await.unwrap();
        let tables: Vec<(String,)> =
            sqlx::query_as("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' ESCAPE '\\' AND name != 'sqlite_sequence' ORDER BY name")
                .fetch_all(&pool)
                .await
                .unwrap();
        let tables: Vec<&str> = tables.iter().map(|t| t.0.as_str()).collect();
        assert_eq!(tables, ["collections", "domains", "jobs", "steps"]);

        let ddl: (String,) = sqlx::query_as("SELECT sql FROM sqlite_master WHERE name = 'steps'")
            .fetch_one(&pool)
            .await
            .unwrap();
        for name in StepName::ALL {
            assert!(
                ddl.0.contains(&format!("'{name}'")),
                "steps.name CHECK misses {name}"
            );
        }
    }
}
