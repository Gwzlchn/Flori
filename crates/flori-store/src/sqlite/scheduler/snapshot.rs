use std::{collections::BTreeSet, fmt::Write};

use super::super::StoreError;
use flori_core::{DomainId, ErrorCode, PromptSnapshot, Sha256Digest};
use sha2::{Digest, Sha256};

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
