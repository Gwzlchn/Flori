use flori_core::ErrorCode;
use sqlx::Row;

use crate::artifact::NasArtifactStore;

use super::{Store, StoreError};

mod attempt;
mod materialize;
mod server_log;

impl Store {
    pub async fn reconcile_uploads(
        &self,
        artifacts: &NasArtifactStore,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let groups = sqlx::query(
            "SELECT DISTINCT owner_kind,owner_id FROM uploads ORDER BY owner_kind,owner_id",
        )
        .fetch_all(&self.pool)
        .await?;
        for row in &groups {
            if !matches!(
                row.try_get::<String, _>("owner_kind")?.as_str(),
                "attempt" | "materialize"
            ) {
                return Err(StoreError::new(ErrorCode::CorruptState));
            }
        }
        for row in groups {
            let owner_id: String = row.try_get("owner_id")?;
            match row.try_get::<String, _>("owner_kind")?.as_str() {
                "attempt" => attempt::reconcile(self, artifacts, &owner_id, now_ms).await?,
                "materialize" => materialize::reconcile(self, artifacts, &owner_id, now_ms).await?,
                _ => unreachable!("owner kinds were validated before recovery"),
            }
        }
        Ok(())
    }
}
