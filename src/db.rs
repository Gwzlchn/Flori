//! SQLite with runtime queries (no query macros). Migrations are embedded and only move forward.
//! Functions that can fail for a caller-visible reason return `sqlx::Result<Result<T, ApiError>>`.
//! Executor writes are conditional on the current state, so a cancel or retry committed by the
//! HTTP layer is never overwritten.

use std::{
    fs, io,
    path::{Path, PathBuf},
    time::Duration,
};

use sqlx::{
    SqliteExecutor, SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous},
};
use time::{OffsetDateTime, format_description::well_known::Rfc3339, macros::offset};

use crate::{
    Config,
    api::{
        ApiError, Collection, Domain, ErrorCode, Feed, JobRef, JobStatus, JobSummary, StepInfo,
        StepStatus,
    },
    pipeline::{
        AiSpec, JOB_FILE, JobSpec, Pipeline, Platform, PromptName, Source, StepName, TranslateMode,
        job_key, parse_feed, plan,
    },
    prompts,
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

/// RFC 3339 in +08:00, whole seconds.
pub fn now() -> String {
    let now = OffsetDateTime::now_utc().to_offset(offset!(+8));
    let now = now.replace_nanosecond(0).unwrap_or(now);
    now.format(&Rfc3339).unwrap_or_default()
}

/// `$DATA/jobs`, holding one directory per job.
pub fn jobs_root(data_dir: &Path) -> PathBuf {
    data_dir.join("jobs")
}

/// `$DATA/jobs/<id>`: job.json, job.log, steps/ and cache/.
pub fn job_dir(data_dir: &Path, id: i64) -> PathBuf {
    jobs_root(data_dir).join(id.to_string())
}

pub(crate) fn not_found(what: &str) -> ApiError {
    ApiError::new(ErrorCode::NotFound, format!("{what} not found"))
}

pub(crate) fn conflict(message: &str) -> ApiError {
    ApiError::new(ErrorCode::Conflict, message)
}

async fn exists(db: impl SqliteExecutor<'_>, table: &str, id: i64) -> sqlx::Result<bool> {
    let sql = format!("SELECT EXISTS (SELECT 1 FROM {table} WHERE id = ?)");
    sqlx::query_scalar(sqlx::AssertSqlSafe(sql))
        .bind(id)
        .fetch_one(db)
        .await
}

/// Deletes one row whose `guard` (SQL that is true while dependents exist) is false.
async fn delete_unused(
    db: &SqlitePool,
    table: &str,
    guard: &str,
    id: i64,
) -> sqlx::Result<Result<(), ApiError>> {
    let sql = format!("DELETE FROM {table} WHERE id = ?1 AND NOT ({guard})");
    let done = sqlx::query(sqlx::AssertSqlSafe(sql))
        .bind(id)
        .execute(db)
        .await?
        .rows_affected();
    Ok(match (done, exists(db, table, id).await?) {
        (1.., _) => Ok(()),
        (0, true) => Err(conflict("still in use")),
        (0, false) => Err(not_found(table.trim_end_matches('s'))),
    })
}

pub async fn list_domains(db: &SqlitePool) -> sqlx::Result<Vec<Domain>> {
    sqlx::query_as("SELECT id, name, created FROM domains ORDER BY name")
        .fetch_all(db)
        .await
}

pub async fn get_domain(db: &SqlitePool, id: i64) -> sqlx::Result<Option<Domain>> {
    sqlx::query_as("SELECT id, name, created FROM domains WHERE id = ?")
        .bind(id)
        .fetch_optional(db)
        .await
}

/// Whether two names give the same vault folder on the devices that sync it: the file systems
/// of phones, macOS and Windows ignore case.
pub fn same_folder(a: &str, b: &str) -> bool {
    a.to_lowercase() == b.to_lowercase()
}

/// Names are compared in a write transaction, so no other insert slips in between.
pub async fn create_domain(db: &SqlitePool, name: &str) -> sqlx::Result<Result<Domain, ApiError>> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    let names: Vec<String> = sqlx::query_scalar("SELECT name FROM domains")
        .fetch_all(&mut *tx)
        .await?;
    if names.iter().any(|taken| same_folder(taken, name)) {
        return Ok(Err(conflict("domain name already exists")));
    }
    let domain: Domain = sqlx::query_as(
        "INSERT INTO domains (name, created) VALUES (?, ?) RETURNING id, name, created",
    )
    .bind(name)
    .bind(now())
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Ok(domain))
}

pub async fn delete_domain(db: &SqlitePool, id: i64) -> sqlx::Result<Result<(), ApiError>> {
    let guard = "EXISTS (SELECT 1 FROM collections WHERE domain_id = ?1) \
                 OR EXISTS (SELECT 1 FROM jobs WHERE domain_id = ?1)";
    delete_unused(db, "domains", guard, id).await
}

const COLUMNS: &str = "id, domain_id, name, feed_url, created";

type CollectionRow = (i64, i64, String, Option<String>, String);

/// `feed_url` is stored in normal form, so parsing it again recovers the kind.
fn collection((id, domain_id, name, feed_url, created): CollectionRow) -> Collection {
    let feed = feed_url
        .as_deref()
        .and_then(parse_feed)
        .map(|(kind, url)| Feed { kind, url });
    Collection {
        id,
        domain_id,
        name,
        feed,
        created,
    }
}

pub async fn list_collections(db: &SqlitePool) -> sqlx::Result<Vec<Collection>> {
    let sql = format!("SELECT {COLUMNS} FROM collections ORDER BY domain_id, name");
    let rows: Vec<CollectionRow> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .fetch_all(db)
        .await?;
    Ok(rows.into_iter().map(collection).collect())
}

pub async fn get_collection(db: &SqlitePool, id: i64) -> sqlx::Result<Option<Collection>> {
    let sql = format!("SELECT {COLUMNS} FROM collections WHERE id = ?");
    let row: Option<CollectionRow> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(db)
        .await?;
    Ok(row.map(collection))
}

/// Unique within the domain the way [`create_domain`] is among domains.
pub async fn create_collection(
    db: &SqlitePool,
    domain_id: i64,
    name: &str,
) -> sqlx::Result<Result<Collection, ApiError>> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    if !exists(&mut *tx, "domains", domain_id).await? {
        return Ok(Err(not_found("domain")));
    }
    let names: Vec<String> = sqlx::query_scalar("SELECT name FROM collections WHERE domain_id = ?")
        .bind(domain_id)
        .fetch_all(&mut *tx)
        .await?;
    if names.iter().any(|taken| same_folder(taken, name)) {
        return Ok(Err(conflict(
            "collection name already exists in this domain",
        )));
    }
    let sql = format!(
        "INSERT INTO collections (domain_id, name, created) VALUES (?, ?, ?) RETURNING {COLUMNS}"
    );
    let row: CollectionRow = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .bind(domain_id)
        .bind(name)
        .bind(now())
        .fetch_one(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(Ok(collection(row)))
}

pub async fn delete_collection(db: &SqlitePool, id: i64) -> sqlx::Result<Result<(), ApiError>> {
    let guard = "EXISTS (SELECT 1 FROM jobs WHERE collection_id = ?1)";
    delete_unused(db, "collections", guard, id).await
}

pub async fn set_feed(
    db: &SqlitePool,
    id: i64,
    feed_url: Option<&str>,
) -> sqlx::Result<Result<Collection, ApiError>> {
    let sql = format!("UPDATE collections SET feed_url = ? WHERE id = ? RETURNING {COLUMNS}");
    let row: Option<CollectionRow> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .bind(feed_url)
        .bind(id)
        .fetch_optional(db)
        .await?;
    Ok(row.map(collection).ok_or_else(|| not_found("collection")))
}

/// The current step is the first one running, failed or canceled; `current_exit_code` is its
/// exit code (30 = credentials expired).
const JOB: &str = "SELECT jobs.id, pipeline, platform, source_id, url, domain_id, collection_id, \
    translate, jobs.status, title, author, published, duration_ms, description, created, updated, \
    vault_path, \
    cur.name AS current_step, cur.exit_code AS current_exit_code FROM jobs \
    LEFT JOIN steps cur ON cur.job_id = jobs.id AND cur.seq = (SELECT min(seq) FROM steps \
    WHERE job_id = jobs.id AND status IN ('running', 'failed', 'canceled'))";

/// `job_key` is not a column. The row's `created` is the string job.json got, so the key equals
/// the one publish wrote.
fn with_key(mut job: JobSummary) -> JobSummary {
    job.job_key = job_key(job.platform, &job.source_id, &job.created);
    job
}

pub async fn list_jobs(db: &SqlitePool, limit: u32) -> sqlx::Result<Vec<JobSummary>> {
    let sql = format!("{JOB} ORDER BY jobs.id DESC LIMIT ?");
    let jobs: Vec<JobSummary> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .bind(limit)
        .fetch_all(db)
        .await?;
    Ok(jobs.into_iter().map(with_key).collect())
}

#[derive(sqlx::FromRow)]
struct JobRow {
    #[sqlx(flatten)]
    job: JobSummary,
    vault_path: Option<String>,
}

/// The job and its `vault_path`.
pub async fn get_job(
    db: &SqlitePool,
    id: i64,
) -> sqlx::Result<Option<(JobSummary, Option<String>)>> {
    let sql = format!("{JOB} WHERE jobs.id = ?");
    let row: Option<JobRow> = sqlx::query_as(sqlx::AssertSqlSafe(sql))
        .bind(id)
        .fetch_optional(db)
        .await?;
    Ok(row.map(|row| (with_key(row.job), row.vault_path)))
}

async fn job_summary(db: &SqlitePool, id: i64) -> sqlx::Result<Result<JobSummary, ApiError>> {
    Ok(get_job(db, id)
        .await?
        .map(|j| j.0)
        .ok_or_else(|| not_found("job")))
}

pub async fn steps(db: &SqlitePool, job_id: i64) -> sqlx::Result<Vec<StepInfo>> {
    sqlx::query_as(
        "SELECT seq, name, status, attempts, exit_code, error, started, finished \
         FROM steps WHERE job_id = ? ORDER BY seq",
    )
    .bind(job_id)
    .fetch_all(db)
    .await
}

/// Inserts one job per source with its steps and `job.json`, in one transaction. Each result is
/// `Ok(new job)` or `Err(existing job)` for a duplicate. A failed `job.json` write rolls back.
pub async fn create_jobs(
    db: &SqlitePool,
    config: &Config,
    cli_version: &str,
    domain_id: i64,
    collection_id: Option<i64>,
    translate: TranslateMode,
    sources: &[Source],
) -> sqlx::Result<Result<Vec<Result<JobRef, JobRef>>, ApiError>> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    let names: Option<(String, Option<String>)> = sqlx::query_as(
        "SELECT d.name, c.name FROM domains d \
         LEFT JOIN collections c ON c.id = ?2 AND c.domain_id = d.id WHERE d.id = ?1",
    )
    .bind(domain_id)
    .bind(collection_id)
    .fetch_optional(&mut *tx)
    .await?;
    let (domain, collection) = match names {
        None => return Ok(Err(not_found("domain"))),
        Some((_, None)) if collection_id.is_some() => return Ok(Err(not_found("collection"))),
        Some(names) => names,
    };
    let created = now();
    let mut results = Vec::new();
    let mut dirs = Vec::new();
    for source in sources {
        let pipeline = match source.platform {
            Platform::Arxiv => Pipeline::Arxiv,
            Platform::Bilibili | Platform::Youtube => Pipeline::Video,
        };
        let id: Option<i64> = sqlx::query_scalar(
            "INSERT INTO jobs (domain_id, collection_id, pipeline, platform, source_id, url, \
             translate, status, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?) \
             ON CONFLICT DO NOTHING RETURNING id",
        )
        .bind(domain_id)
        .bind(collection_id)
        .bind(pipeline)
        .bind(source.platform)
        .bind(&source.id)
        .bind(&source.url)
        .bind(translate)
        .bind(&created)
        .bind(&created)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(id) = id else {
            let existing: JobRef = sqlx::query_as(
                "SELECT id, status FROM jobs WHERE domain_id = ? AND collection_id IS ? \
                 AND platform = ? AND source_id = ?",
            )
            .bind(domain_id)
            .bind(collection_id)
            .bind(source.platform)
            .bind(&source.id)
            .fetch_one(&mut *tx)
            .await?;
            results.push(Err(existing));
            continue;
        };
        for (seq, step) in (1..).zip(plan(pipeline, translate)) {
            sqlx::query(
                "INSERT INTO steps (job_id, seq, name, status) VALUES (?, ?, ?, 'pending')",
            )
            .bind(id)
            .bind(seq)
            .bind(step.name)
            .execute(&mut *tx)
            .await?;
        }
        let spec = JobSpec {
            job_id: id,
            pipeline,
            source: source.clone(),
            domain: domain.clone(),
            collection: collection.clone(),
            translate,
            ai: AiSpec {
                tool: config.ai_tool,
                model_note: config.ai_model_note.clone(),
                model_translate: config.ai_model_translate.clone(),
                cli_version: cli_version.to_owned(),
            },
            created: created.clone(),
            public_url: config.public_url.clone(),
            vault_name: config.vault_name.clone(),
        };
        let dir = job_dir(&config.data_dir, id);
        dirs.push(dir.clone());
        if let Err(err) = write_job(&dir, &spec) {
            remove_dirs(&dirs);
            return Err(sqlx::Error::Io(err));
        }
        results.push(Ok(JobRef {
            id,
            status: JobStatus::Queued,
        }));
    }
    if let Err(err) = tx.commit().await {
        remove_dirs(&dirs);
        return Err(err);
    }
    Ok(Ok(results))
}

/// The directory may already exist: a crash between this write and the commit can leave one
/// behind for an id that SQLite hands out again.
fn write_job(dir: &Path, spec: &JobSpec) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    let mut text = serde_json::to_string_pretty(spec).map_err(io::Error::other)?;
    text.push('\n');
    fs::write(dir.join(JOB_FILE), text)
}

fn remove_dirs(dirs: &[PathBuf]) {
    for dir in dirs {
        let _ = fs::remove_dir_all(dir);
    }
}

/// failed or canceled -> queued; failed or canceled steps -> pending, keeping their attempts.
pub async fn retry_job(db: &SqlitePool, id: i64) -> sqlx::Result<Result<JobSummary, ApiError>> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    let moved = sqlx::query(
        "UPDATE jobs SET status = 'queued', updated = ? WHERE id = ? AND status IN ('failed', 'canceled')",
    )
    .bind(now())
    .bind(id)
    .execute(&mut *tx)
    .await?;
    if moved.rows_affected() == 0 {
        return Ok(Err(refused(&mut *tx, id).await?));
    }
    sqlx::query(
        "UPDATE steps SET status = 'pending', exit_code = NULL, error = NULL, started = NULL, \
         finished = NULL WHERE job_id = ? AND status IN ('failed', 'canceled')",
    )
    .bind(id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    job_summary(db, id).await
}

/// queued or running -> canceled, together with the running step.
pub async fn cancel_job(db: &SqlitePool, id: i64) -> sqlx::Result<Result<JobSummary, ApiError>> {
    let now = now();
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    let moved = sqlx::query(
        "UPDATE jobs SET status = 'canceled', updated = ? WHERE id = ? AND status IN ('queued', 'running')",
    )
    .bind(&now)
    .bind(id)
    .execute(&mut *tx)
    .await?;
    if moved.rows_affected() == 0 {
        return Ok(Err(refused(&mut *tx, id).await?));
    }
    sqlx::query("UPDATE steps SET status = 'canceled', finished = ? WHERE job_id = ? AND status = 'running'")
        .bind(&now)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    job_summary(db, id).await
}

/// Runs on the transaction's own connection: the write lock is held, so asking the pool for
/// another connection could wait on the very writers that wait on us.
async fn refused(db: impl SqliteExecutor<'_>, id: i64) -> sqlx::Result<ApiError> {
    Ok(if exists(db, "jobs", id).await? {
        conflict("job is not in a state that allows this")
    } else {
        not_found("job")
    })
}

pub async fn queued_jobs(db: &SqlitePool) -> sqlx::Result<Vec<i64>> {
    sqlx::query_scalar("SELECT id FROM jobs WHERE status = 'queued' ORDER BY id")
        .fetch_all(db)
        .await
}

/// queued -> running. False when the job was canceled before it started.
pub async fn claim_job(db: &SqlitePool, id: i64) -> sqlx::Result<bool> {
    let done = sqlx::query(
        "UPDATE jobs SET status = 'running', updated = ? WHERE id = ? AND status = 'queued'",
    )
    .bind(now())
    .bind(id)
    .execute(db)
    .await?;
    Ok(done.rows_affected() == 1)
}

pub async fn job_steps(db: &SqlitePool, id: i64) -> sqlx::Result<Vec<(u32, StepName, StepStatus)>> {
    sqlx::query_as("SELECT seq, name, status FROM steps WHERE job_id = ? ORDER BY seq")
        .bind(id)
        .fetch_all(db)
        .await
}

/// Starts one attempt and returns the step's attempt count, or None when the job is no longer
/// running or the step was canceled.
pub async fn start_attempt(db: &SqlitePool, job_id: i64, seq: u32) -> sqlx::Result<Option<u32>> {
    sqlx::query_scalar(
        "UPDATE steps SET status = 'running', attempts = attempts + 1, started = ?3, finished = NULL \
         WHERE job_id = ?1 AND seq = ?2 AND status IN ('pending', 'running') \
         AND (SELECT status FROM jobs WHERE id = ?1) = 'running' RETURNING attempts",
    )
    .bind(job_id)
    .bind(seq)
    .bind(now())
    .fetch_optional(db)
    .await
}

/// Records the end of an attempt on a running step. `Running` keeps the step running between
/// automatic retries. False when the HTTP layer already moved the step.
pub async fn end_attempt(
    db: &SqlitePool,
    job_id: i64,
    seq: u32,
    status: StepStatus,
    exit_code: Option<i32>,
    error: Option<&str>,
) -> sqlx::Result<bool> {
    let finished = (status != StepStatus::Running).then(now);
    let done = sqlx::query(
        "UPDATE steps SET status = ?, exit_code = ?, error = ?, finished = ? \
         WHERE job_id = ? AND seq = ? AND status = 'running'",
    )
    .bind(status)
    .bind(exit_code)
    .bind(error)
    .bind(finished)
    .bind(job_id)
    .bind(seq)
    .execute(db)
    .await?;
    Ok(done.rows_affected() == 1)
}

/// running -> succeeded or failed; a canceled job stays canceled.
pub async fn end_job(db: &SqlitePool, id: i64, status: JobStatus) -> sqlx::Result<()> {
    sqlx::query("UPDATE jobs SET status = ?, updated = ? WHERE id = ? AND status = 'running'")
        .bind(status)
        .bind(now())
        .bind(id)
        .execute(db)
        .await?;
    Ok(())
}

/// Fails the job and its running step after an executor error outside any step.
pub async fn fail_job(db: &SqlitePool, id: i64, error: &str) -> sqlx::Result<()> {
    sqlx::query(
        "UPDATE steps SET status = 'failed', error = ?, finished = ? WHERE job_id = ? AND status = 'running'",
    )
    .bind(error)
    .bind(now())
    .bind(id)
    .execute(db)
    .await?;
    end_job(db, id, JobStatus::Failed).await
}

/// What a succeeded publish step leaves on its job: where the note is, and the columns the note
/// list shows without opening any manifest.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Published {
    pub vault_path: String,
    pub author: Option<String>,
    pub published: Option<String>,
    pub duration_ms: Option<i64>,
    pub description: Option<String>,
}

/// running -> succeeded, together with the job's first title and what publish left, in one
/// transaction so a crash cannot keep a finished step without its output. False when the HTTP
/// layer already moved the step.
pub async fn step_succeeded(
    db: &SqlitePool,
    job_id: i64,
    seq: u32,
    title: Option<&str>,
    published: Option<&Published>,
) -> sqlx::Result<bool> {
    let now = now();
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    let done = sqlx::query(
        "UPDATE steps SET status = 'succeeded', exit_code = 0, error = NULL, finished = ? \
         WHERE job_id = ? AND seq = ? AND status = 'running'",
    )
    .bind(&now)
    .bind(job_id)
    .bind(seq)
    .execute(&mut *tx)
    .await?;
    if done.rows_affected() == 0 {
        return Ok(false);
    }
    sqlx::query("UPDATE jobs SET title = ifnull(title, ?), updated = ? WHERE id = ?")
        .bind(title)
        .bind(&now)
        .bind(job_id)
        .execute(&mut *tx)
        .await?;
    if let Some(note) = published {
        sqlx::query(
            "UPDATE jobs SET vault_path = ?, author = ?, published = ?, duration_ms = ?, \
             description = ? WHERE id = ?",
        )
        .bind(&note.vault_path)
        .bind(&note.author)
        .bind(&note.published)
        .bind(note.duration_ms)
        .bind(&note.description)
        .bind(job_id)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(true)
}

/// The stored prompt overrides and, with `domain_id`, that domain's requirements.
pub async fn prompts(db: &SqlitePool, domain_id: Option<i64>) -> sqlx::Result<prompts::Stored> {
    let rows: Vec<(PromptName, Option<i64>, String)> = sqlx::query_as(
        "SELECT name, domain_id, text FROM prompts WHERE domain_id IS NULL OR domain_id = ?",
    )
    .bind(domain_id)
    .fetch_all(db)
    .await?;
    let mut stored = prompts::Stored::default();
    for (name, domain, text) in rows {
        let texts = match domain {
            Some(_) => &mut stored.domain,
            None => &mut stored.overrides,
        };
        texts.insert(name, text);
    }
    Ok(stored)
}

/// Stores the override of `name`, or with `domain_id` that domain's requirement; None deletes
/// it. A requirement goes with its domain: the foreign key cascades.
pub async fn set_prompt(
    db: &SqlitePool,
    name: PromptName,
    domain_id: Option<i64>,
    text: Option<&str>,
) -> sqlx::Result<Result<(), ApiError>> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    if let Some(id) = domain_id
        && !exists(&mut *tx, "domains", id).await?
    {
        return Ok(Err(not_found("domain")));
    }
    sqlx::query("DELETE FROM prompts WHERE name = ? AND domain_id IS ?")
        .bind(name)
        .bind(domain_id)
        .execute(&mut *tx)
        .await?;
    if let Some(text) = text {
        sqlx::query("INSERT INTO prompts (name, domain_id, text, updated) VALUES (?, ?, ?, ?)")
            .bind(name)
            .bind(domain_id)
            .bind(text)
            .bind(now())
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(Ok(()))
}

/// Numbers of domains, collections and published notes.
pub async fn counts(db: &SqlitePool) -> sqlx::Result<(u32, u32, u32)> {
    sqlx::query_as(
        "SELECT (SELECT count(*) FROM domains), (SELECT count(*) FROM collections), \
         (SELECT count(*) FROM jobs WHERE status = 'succeeded')",
    )
    .fetch_one(db)
    .await
}

/// Jobs created per day from `first_day` (`YYYY-MM-DD`) on. `created` is always written in
/// +08:00, so its first ten characters are the local date and compare as text.
pub async fn submissions_since(
    db: &SqlitePool,
    first_day: &str,
) -> sqlx::Result<Vec<(String, u32)>> {
    sqlx::query_as(
        "SELECT substr(created, 1, 10) AS day, count(*) FROM jobs WHERE created >= ? GROUP BY day",
    )
    .bind(first_day)
    .fetch_all(db)
    .await
}

/// After a crash nothing is running: running jobs go back to the queue and running steps to
/// pending.
pub async fn recover(db: &SqlitePool) -> sqlx::Result<()> {
    let mut tx = db.begin_with("BEGIN IMMEDIATE").await?;
    sqlx::query("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        .execute(&mut *tx)
        .await?;
    sqlx::query("UPDATE steps SET status = 'pending' WHERE status = 'running'")
        .execute(&mut *tx)
        .await?;
    tx.commit().await
}

#[cfg(test)]
mod tests;
