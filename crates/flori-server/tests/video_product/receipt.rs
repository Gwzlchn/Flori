use std::{fmt::Write as _, fs, path::Path};

use flori_core::{JobId, Sha256Digest, SourceId};
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

use super::platform::Case;

pub(super) async fn write(
    pool: &SqlitePool,
    artifact_root: &Path,
    source_id: SourceId,
    job_id: JobId,
    case: Case,
    root: &Path,
) {
    let rows: Vec<(String, String, String, i64, String)> = sqlx::query_as(
        "SELECT id,kind,relative_path,size_bytes,sha256 FROM artifacts WHERE job_id=? ORDER BY kind,name,id",
    )
    .bind(job_id.to_string())
    .fetch_all(pool)
    .await
    .expect("receipt artifacts");
    let mut artifacts = Vec::with_capacity(rows.len());
    for (id, kind, relative, size, expected_sha) in rows {
        let path = artifact_root.join(&relative);
        let bytes = fs::read(&path).expect("receipt artifact bytes");
        assert_eq!(i64::try_from(bytes.len()).expect("artifact size"), size);
        assert_eq!(digest(&bytes).as_str(), expected_sha);
        artifacts.push(serde_json::json!({
            "artifact_id": id,
            "kind": kind,
            "path": fs::canonicalize(path).expect("absolute artifact path"),
            "size_bytes": size,
            "sha256": expected_sha,
        }));
    }
    let whisper_calls = fs::read(root.join("whisper-called")).map_or(0, |bytes| bytes.len());
    let receipt = serde_json::json!({
        "case": format!("{case:?}").to_lowercase(),
        "source_id": source_id,
        "job_id": job_id,
        "current": true,
        "whisper_calls": whisper_calls,
        "artifacts": artifacts,
    });
    fs::write(
        root.join("receipt.json"),
        serde_json::to_vec_pretty(&receipt).expect("receipt JSON"),
    )
    .expect("write receipt");
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("String");
    }
    Sha256Digest::parse(value).expect("digest")
}
