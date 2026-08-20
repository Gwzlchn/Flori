use std::{collections::BTreeSet, fmt::Write};

use super::super::StoreError;
use flori_core::{
    DomainId, ErrorCode, PromptSnapshot, PromptSnapshotProfile, PromptSnapshotPrompt, Sha256Digest,
};
use sha2::{Digest, Sha256};
use sqlx::{Row, Sqlite, Transaction};

pub(super) async fn current_prompt_snapshot(
    transaction: &mut Transaction<'_, Sqlite>,
    domain_id: DomainId,
    required_prompts: &BTreeSet<&str>,
) -> Result<PromptSnapshot, StoreError> {
    let profile_text: String = sqlx::query_scalar("SELECT profile_text FROM domains WHERE id=?")
        .bind(domain_id.to_string())
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or_else(corrupt)?;
    let rows = sqlx::query("SELECT key,content,sha256 FROM prompts ORDER BY key")
        .fetch_all(&mut **transaction)
        .await?;
    let mut prompts = Vec::with_capacity(required_prompts.len());
    for row in rows {
        let key: String = row.try_get("key")?;
        if required_prompts.contains(key.as_str()) {
            let content: String = row.try_get("content")?;
            let sha256 =
                Sha256Digest::parse(row.try_get::<String, _>("sha256")?).map_err(|_| corrupt())?;
            if sha256 != digest(&content) {
                return Err(corrupt());
            }
            prompts.push(PromptSnapshotPrompt {
                key,
                content,
                sha256,
            });
        }
    }
    if prompts.len() != required_prompts.len() {
        return Err(corrupt());
    }
    Ok(PromptSnapshot {
        profile: PromptSnapshotProfile {
            domain_id,
            sha256: digest(&profile_text),
            profile_text,
        },
        prompts,
    })
}

pub(super) fn freeze_prompt_snapshot(
    snapshot: &PromptSnapshot,
    domain_id: DomainId,
    required_prompts: &BTreeSet<&str>,
) -> Result<(String, Sha256Digest), StoreError> {
    let ordered = snapshot
        .prompts
        .windows(2)
        .all(|pair| pair[0].key < pair[1].key);
    let present = snapshot
        .prompts
        .iter()
        .map(|prompt| prompt.key.as_str())
        .collect::<BTreeSet<_>>();
    let valid_hashes = snapshot.profile.sha256 == digest(&snapshot.profile.profile_text)
        && snapshot
            .prompts
            .iter()
            .all(|prompt| prompt.sha256 == digest(&prompt.content));
    if snapshot.profile.domain_id != domain_id
        || !ordered
        || present != *required_prompts
        || !valid_hashes
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    let json =
        serde_json::to_string(snapshot).map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?;
    Ok((json.clone(), digest(&json)))
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(output).expect("SHA-256 formatter is canonical")
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
