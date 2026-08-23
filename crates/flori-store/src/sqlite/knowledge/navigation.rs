use std::{collections::BTreeMap, str::FromStr};

use flori_core::{
    CollectionId, CollectionKind, CollectionView, DomainId, DomainView, ErrorCode, SourceId,
    SourceView,
};
use sqlx::{Row, sqlite::SqliteRow};

use super::{
    super::{Store, StoreError},
    detail::parse_source,
};

impl Store {
    pub async fn list_domains(&self) -> Result<Vec<DomainView>, StoreError> {
        let rows = sqlx::query(
            "SELECT d.id,d.slug,d.name,d.description,d.profile_text, \
             (SELECT count(*) FROM collections c WHERE c.domain_id=d.id) AS collection_count, \
             (SELECT count(*) FROM sources s WHERE s.domain_id=d.id) AS source_count \
             FROM domains d ORDER BY lower(d.name),d.id",
        )
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(parse_domain).collect()
    }

    pub async fn list_collections(&self) -> Result<Vec<CollectionView>, StoreError> {
        let rows = sqlx::query(
            "SELECT c.id,c.domain_id,c.name,c.kind,c.subscription_source_id,s.domain_id \
             AS subscription_domain_id,c.enabled, \
             c.fanout_limit,c.last_synced_at_ms,c.last_sync_error, \
             (SELECT count(*) FROM collection_sources cs WHERE cs.collection_id=c.id) \
             AS source_count FROM collections c LEFT JOIN sources s ON s.id=c.subscription_source_id \
             ORDER BY c.domain_id,lower(c.name),c.id",
        )
        .fetch_all(&self.pool)
        .await?;
        rows.iter().map(parse_collection).collect()
    }

    pub async fn list_sources(&self) -> Result<Vec<SourceView>, StoreError> {
        let rows = sqlx::query(
            "SELECT s.id,s.kind,s.canonical_ref,s.title,s.domain_id,s.current_job_id, \
             s.previous_job_id,c.source_id AS current_source_id,c.state AS current_state, \
             p.source_id AS previous_source_id,p.state AS previous_state FROM sources s \
             LEFT JOIN jobs c ON c.id=s.current_job_id LEFT JOIN jobs p ON p.id=s.previous_job_id \
             ORDER BY lower(COALESCE(NULLIF(s.title,''),s.canonical_ref)),s.id",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut sources = rows
            .iter()
            .map(parse_source)
            .collect::<Result<Vec<_>, _>>()?;
        let memberships = self.source_memberships().await?;
        for source in &mut sources {
            source.collection_ids = memberships
                .get(&source.source_id)
                .cloned()
                .unwrap_or_default();
        }
        if memberships
            .keys()
            .any(|source_id| !sources.iter().any(|source| source.source_id == *source_id))
        {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
        Ok(sources)
    }

    pub(super) async fn source_collection_ids(
        &self,
        source_id: SourceId,
    ) -> Result<Vec<CollectionId>, StoreError> {
        Ok(self
            .source_memberships()
            .await?
            .remove(&source_id)
            .unwrap_or_default())
    }

    async fn source_memberships(
        &self,
    ) -> Result<BTreeMap<SourceId, Vec<CollectionId>>, StoreError> {
        let rows = sqlx::query(
            "SELECT cs.source_id,cs.collection_id,s.domain_id AS source_domain_id, \
             c.domain_id AS collection_domain_id FROM collection_sources cs \
             JOIN sources s ON s.id=cs.source_id JOIN collections c ON c.id=cs.collection_id \
             ORDER BY cs.source_id,cs.collection_id",
        )
        .fetch_all(&self.pool)
        .await?;
        let mut memberships = BTreeMap::new();
        for row in &rows {
            if parse_id::<DomainId>(row, "source_domain_id")?
                != parse_id::<DomainId>(row, "collection_domain_id")?
            {
                return Err(StoreError::new(ErrorCode::CorruptState));
            }
            memberships
                .entry(parse_id(row, "source_id")?)
                .or_insert_with(Vec::new)
                .push(parse_id(row, "collection_id")?);
        }
        Ok(memberships)
    }
}

fn parse_domain(row: &SqliteRow) -> Result<DomainView, StoreError> {
    Ok(DomainView {
        domain_id: parse_id(row, "id")?,
        slug: row.try_get("slug")?,
        name: row.try_get("name")?,
        description: row.try_get("description")?,
        profile_text: row.try_get("profile_text")?,
        collection_count: required_u64(row, "collection_count")?,
        source_count: required_u64(row, "source_count")?,
    })
}

fn parse_collection(row: &SqliteRow) -> Result<CollectionView, StoreError> {
    let enabled = row.try_get::<i64, _>("enabled")?;
    let domain_id = parse_id(row, "domain_id")?;
    let subscription_source_id = parse_optional_id(row, "subscription_source_id")?;
    let subscription_domain_id = parse_optional_id::<DomainId>(row, "subscription_domain_id")?;
    if subscription_source_id.is_some() != subscription_domain_id.is_some()
        || subscription_domain_id.is_some_and(|value| value != domain_id)
    {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    Ok(CollectionView {
        collection_id: parse_id(row, "id")?,
        domain_id,
        name: row.try_get("name")?,
        kind: parse_collection_kind(&row.try_get::<String, _>("kind")?)?,
        subscription_source_id,
        enabled: match enabled {
            0 => false,
            1 => true,
            _ => return Err(StoreError::new(ErrorCode::CorruptState)),
        },
        fanout_limit: optional_u32(row, "fanout_limit")?,
        last_synced_at_ms: optional_u64(row, "last_synced_at_ms")?,
        last_sync_error: row.try_get("last_sync_error")?,
        source_count: required_u64(row, "source_count")?,
    })
}

fn parse_id<T: FromStr>(row: &SqliteRow, column: &str) -> Result<T, StoreError> {
    row.try_get::<String, _>(column)?
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn parse_optional_id<T: FromStr>(row: &SqliteRow, column: &str) -> Result<Option<T>, StoreError> {
    row.try_get::<Option<String>, _>(column)?
        .map(|value| {
            value
                .parse()
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))
        })
        .transpose()
}

fn parse_collection_kind(value: &str) -> Result<CollectionKind, StoreError> {
    let json =
        serde_json::to_string(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
    serde_json::from_str(&json).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u64(row: &SqliteRow, column: &str) -> Result<u64, StoreError> {
    u64::try_from(row.try_get::<i64, _>(column)?)
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn optional_u64(row: &SqliteRow, column: &str) -> Result<Option<u64>, StoreError> {
    row.try_get::<Option<i64>, _>(column)?
        .map(|value| u64::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState)))
        .transpose()
}

fn optional_u32(row: &SqliteRow, column: &str) -> Result<Option<u32>, StoreError> {
    row.try_get::<Option<i64>, _>(column)?
        .map(|value| u32::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState)))
        .transpose()
}
