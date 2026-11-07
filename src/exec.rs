//! Executor: one tokio task per job with steps in order, global limits, child process groups with
//! timeouts, line-by-line job.log, env whitelist, cancel, and recovery on start.

use std::sync::Arc;

use sqlx::SqlitePool;
use tokio::sync::Notify;

use crate::Config;

/// Cheap handle the HTTP layer uses after it commits a state change.
#[derive(Clone)]
pub struct ExecHandle {
    wake: Arc<Notify>,
}

impl ExecHandle {
    /// New or retried jobs are queued in the database; this only nudges the scheduler.
    pub fn wake(&self) {
        self.wake.notify_one();
    }

    /// Kills the process group of the running step. The caller has already marked the job canceled.
    pub fn cancel(&self, _job_id: i64) {}
}

/// Skeleton stub: accepts no work yet.
pub async fn start(_db: SqlitePool, _config: Arc<Config>) -> Result<ExecHandle, String> {
    Ok(ExecHandle {
        wake: Arc::new(Notify::new()),
    })
}
