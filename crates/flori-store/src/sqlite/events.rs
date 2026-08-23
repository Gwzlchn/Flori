use flori_core::{ErrorCode, JobEvent, JobEventPayload, JobId, SystemHealthStatus, SystemView};
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{Store, StoreError};

impl Store {
    pub async fn append_event(
        &self,
        payload: &JobEventPayload,
        now_ms: i64,
    ) -> Result<u64, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let (scope, scope_id, kind, data) = event_parts(payload)?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let result = sqlx::query(
            "INSERT INTO job_events(scope,scope_id,kind,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
        )
        .bind(scope)
        .bind(scope_id)
        .bind(kind)
        .bind(data)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        prune_events(&mut transaction).await?;
        transaction.commit().await?;
        result.last_insert_rowid().try_into().map_err(|_| corrupt())
    }

    pub async fn read_events(
        &self,
        job_id: Option<JobId>,
        after: u64,
        limit: u16,
    ) -> Result<Vec<JobEvent>, StoreError> {
        if limit == 0 || limit > 256 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let after_i64 = i64::try_from(after).map_err(|_| invalid())?;
        let scope_id = job_id.map(|id| id.to_string());
        let minimum: Option<i64> = match &scope_id {
            Some(id) => {
                sqlx::query_scalar(
                    "SELECT min(id) FROM job_events WHERE scope='job' AND scope_id=?",
                )
                .bind(id)
                .fetch_one(&self.pool)
                .await?
            }
            None => {
                sqlx::query_scalar("SELECT min(id) FROM job_events")
                    .fetch_one(&self.pool)
                    .await?
            }
        };
        let allocated: i64 = sqlx::query_scalar(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='job_events'),0)",
        )
        .fetch_one(&self.pool)
        .await?;
        if after_i64 > 0
            && minimum.map_or(after_i64 <= allocated, |minimum| after_i64 + 1 < minimum)
        {
            return Err(StoreError::new(ErrorCode::EventCursorExpired));
        }
        let rows = match &scope_id {
            Some(id) => {
                sqlx::query(
                    "SELECT id,kind,payload_json,created_at_ms FROM job_events \
                 WHERE scope='job' AND scope_id=? AND id>? ORDER BY id LIMIT ?",
                )
                .bind(id)
                .bind(after_i64)
                .bind(i64::from(limit))
                .fetch_all(&self.pool)
                .await?
            }
            None => {
                sqlx::query(
                    "SELECT id,kind,payload_json,created_at_ms FROM job_events \
                 WHERE id>? ORDER BY id LIMIT ?",
                )
                .bind(after_i64)
                .bind(i64::from(limit))
                .fetch_all(&self.pool)
                .await?
            }
        };
        rows.into_iter().map(parse_event).collect()
    }

    pub async fn system_view(
        &self,
        artifacts: &NasArtifactStore,
        online_after_ms: i64,
    ) -> Result<SystemView, StoreError> {
        if online_after_ms < 0 {
            return Err(invalid());
        }
        let queue_depth = count(
            &self.pool,
            "SELECT count(*) FROM jobs WHERE state IN ('queued','running')",
        )
        .await?;
        let runners_total = count(&self.pool, "SELECT count(*) FROM runners").await?;
        let runners_online: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM runners WHERE state='enabled' AND last_seen_at_ms>=?",
        )
        .bind(online_after_ms)
        .fetch_one(&self.pool)
        .await?;
        let usage_started = count(
            &self.pool,
            "SELECT count(*) FROM ai_usage WHERE state='started'",
        )
        .await?;
        let usage_final = count(
            &self.pool,
            "SELECT count(*) FROM ai_usage WHERE state='final'",
        )
        .await?;
        let disk_free_bytes = artifacts
            .disk_free_bytes()
            .map_err(|error| StoreError::new(error.code()))?;
        Ok(SystemView {
            status: if disk_free_bytes >= artifacts.max_size_bytes() {
                SystemHealthStatus::Healthy
            } else {
                SystemHealthStatus::Degraded
            },
            queue_depth,
            disk_free_bytes,
            runners_total,
            runners_online: runners_online.try_into().map_err(|_| corrupt())?,
            usage_started,
            usage_final,
        })
    }
}

async fn count(pool: &sqlx::SqlitePool, query: &'static str) -> Result<u64, StoreError> {
    let value: i64 = sqlx::query_scalar(query).fetch_one(pool).await?;
    value.try_into().map_err(|_| corrupt())
}

fn parse_event(row: sqlx::sqlite::SqliteRow) -> Result<JobEvent, StoreError> {
    let kind: String = row.try_get("kind")?;
    let json: String = row.try_get("payload_json")?;
    macro_rules! parsed {
        ($variant:ident) => {
            JobEventPayload::$variant(serde_json::from_str(&json).map_err(|_| corrupt())?)
        };
    }
    let payload = match kind.as_str() {
        "source_changed" => parsed!(SourceChanged),
        "job_state" => parsed!(JobState),
        "task_state" => parsed!(TaskState),
        "artifact_committed" => parsed!(ArtifactCommitted),
        "log_cursor" => parsed!(LogCursor),
        "runner_changed" => parsed!(RunnerChanged),
        "system_health" => parsed!(SystemHealth),
        _ => return Err(corrupt()),
    };
    Ok(JobEvent {
        id: row
            .try_get::<i64, _>("id")?
            .try_into()
            .map_err(|_| corrupt())?,
        created_at_ms: row
            .try_get::<i64, _>("created_at_ms")?
            .try_into()
            .map_err(|_| corrupt())?,
        payload,
    })
}

fn event_parts(
    payload: &JobEventPayload,
) -> Result<(&'static str, Option<String>, &'static str, String), StoreError> {
    macro_rules! part {
        ($scope:literal, $id:expr, $kind:literal, $value:expr) => {
            (
                $scope,
                $id,
                $kind,
                serde_json::to_string($value).map_err(|_| corrupt())?,
            )
        };
    }
    Ok(match payload {
        JobEventPayload::SourceChanged(value) => part!(
            "source",
            Some(value.source_id.to_string()),
            "source_changed",
            value
        ),
        JobEventPayload::JobState(value) => {
            part!("job", Some(value.job_id.to_string()), "job_state", value)
        }
        JobEventPayload::TaskState(value) => {
            part!("job", Some(value.job_id.to_string()), "task_state", value)
        }
        JobEventPayload::ArtifactCommitted(value) => part!(
            "job",
            Some(value.job_id.to_string()),
            "artifact_committed",
            value
        ),
        JobEventPayload::LogCursor(value) => {
            part!("job", Some(value.job_id.to_string()), "log_cursor", value)
        }
        JobEventPayload::RunnerChanged(value) => part!(
            "runner",
            Some(value.runner_id.to_string()),
            "runner_changed",
            value
        ),
        JobEventPayload::SystemHealth(value) => part!("system", None, "system_health", value),
    })
}

async fn prune_events(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
) -> Result<(), StoreError> {
    sqlx::query("DELETE FROM job_events WHERE scope='job' AND NOT EXISTS(SELECT 1 FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.id=job_events.scope_id AND (j.state IN ('queued','running') OR j.id IN (COALESCE(s.current_job_id,''),COALESCE(s.previous_job_id,'')) OR (j.state='failed' AND j.id=(SELECT id FROM jobs f WHERE f.source_id=j.source_id AND f.state='failed' ORDER BY f.finished_at_ms DESC,f.id DESC LIMIT 1))))")
        .execute(&mut **transaction).await?;
    for scope in ["runner", "system"] {
        sqlx::query("DELETE FROM job_events WHERE scope=? AND id NOT IN (SELECT id FROM job_events WHERE scope=? ORDER BY id DESC LIMIT 10000)")
            .bind(scope).bind(scope).execute(&mut **transaction).await?;
    }
    Ok(())
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::InvalidRequest)
}
fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
