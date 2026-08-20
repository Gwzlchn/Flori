use flori_core::Sha256Digest;
use sha2::{Digest, Sha256};

pub(crate) fn sha256(bytes: &[u8]) -> Result<Sha256Digest, &'static str> {
    let text = Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    Sha256Digest::parse(text)
}
