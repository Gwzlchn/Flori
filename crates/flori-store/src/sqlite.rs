use std::{fmt, path::Path};

use flori_core::{
    AiTool, AiUsageId, AttemptId, CONTRACT_REVISION, ErrorCode, JobId, TaskId, UsageOrigin,
};
use sqlx::{
    Connection, Executor, Row, SqliteConnection, SqlitePool,
    sqlite::{SqliteConnectOptions, SqlitePoolOptions},
};

const SCHEMA: &str = include_str!("../migrations/0001_v1.sql");

mod lease;
mod reconcile;
mod runner;
mod scheduler;
mod usage;

pub use scheduler::{CreateJob, CreateSource};

#[derive(Debug)]
pub struct StoreError {
    code: ErrorCode,
    source: Option<sqlx::Error>,
}

impl StoreError {
    fn new(code: ErrorCode) -> Self {
        Self { code, source: None }
    }

    #[must_use]
    pub const fn code(&self) -> ErrorCode {
        self.code
    }
}

impl fmt::Display for StoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.source {
            Some(source) => write!(formatter, "SQLite operation failed: {source}"),
            None => write!(formatter, "SQLite rejected operation: {:?}", self.code),
        }
    }
}

impl std::error::Error for StoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source
            .as_ref()
            .map(|source| source as &(dyn std::error::Error + 'static))
    }
}

impl From<sqlx::Error> for StoreError {
    fn from(source: sqlx::Error) -> Self {
        Self {
            code: ErrorCode::Internal,
            source: Some(source),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Lease {
    pub attempt_id: AttemptId,
    pub lease_expires_at_ms: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct StartAiUsage<'a> {
    pub id: AiUsageId,
    pub job_id: JobId,
    pub task_id: TaskId,
    pub attempt_id: AttemptId,
    pub invocation_key: &'a str,
    pub tool: AiTool,
    pub model: &'a str,
    pub effort: &'a str,
    pub created_at_ms: i64,
}

#[derive(Clone, Copy, Debug)]
pub struct FinalAiUsage<'a> {
    pub attempt_id: AttemptId,
    pub invocation_key: &'a str,
    pub origin: UsageOrigin,
    pub input_tokens: Option<i64>,
    pub output_tokens: Option<i64>,
    pub cost_micros: Option<i64>,
    pub credits_micros: Option<i64>,
    pub finalized_at_ms: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct UsageRecord {
    pub id: AiUsageId,
    pub is_final: bool,
    pub applied: bool,
}

pub struct Store {
    pool: SqlitePool,
}

impl Store {
    pub async fn open(path: impl AsRef<Path>) -> Result<Self, StoreError> {
        let options = SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true)
            .foreign_keys(true);
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await?;

        let store = Self { pool };
        store.initialize_or_verify().await?;
        sqlx::query("PRAGMA journal_mode = WAL")
            .execute(&store.pool)
            .await?;
        Ok(store)
    }

    async fn initialize_or_verify(&self) -> Result<(), StoreError> {
        // Compile the bundled schema in isolation before touching an empty on-disk database.
        let mut expected = SqliteConnection::connect("sqlite::memory:").await?;
        expected.execute(SCHEMA).await?;
        let expected_schema = read_schema(&mut expected).await?;

        let count: i64 =
            sqlx::query_scalar("SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
                .fetch_one(&self.pool)
                .await?;
        if count == 0 {
            self.pool.execute(SCHEMA).await?;
        }

        let actual_schema = read_schema(&self.pool).await?;
        if actual_schema != expected_schema {
            return Err(StoreError::new(ErrorCode::SchemaMismatch));
        }

        let rows: Vec<(i64, String)> =
            sqlx::query_as("SELECT version, contract_revision FROM schema_meta")
                .fetch_all(&self.pool)
                .await
                .map_err(|_| StoreError::new(ErrorCode::SchemaMismatch))?;
        if rows.as_slice() != [(1, CONTRACT_REVISION.to_owned())] {
            return Err(StoreError::new(ErrorCode::SchemaMismatch));
        }
        Ok(())
    }
}

async fn read_schema<'e, E>(
    executor: E,
) -> Result<Vec<(String, String, String, String)>, StoreError>
where
    E: Executor<'e, Database = sqlx::Sqlite>,
{
    let rows = sqlx::query(
        "SELECT type, name, tbl_name, sql FROM sqlite_schema \
         WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name",
    )
    .fetch_all(executor)
    .await?;
    rows.into_iter()
        .map(|row| {
            Ok((
                row.try_get("type")?,
                row.try_get("name")?,
                row.try_get("tbl_name")?,
                row.try_get("sql")?,
            ))
        })
        .collect::<Result<_, sqlx::Error>>()
        .map_err(Into::into)
}
