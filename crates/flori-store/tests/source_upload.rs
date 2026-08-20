use std::{fmt::Write, fs, path::PathBuf};

use flori_core::{CreateUploadSource, DomainId, ErrorCode, JobId, Sha256Digest, SourceKind};
use flori_store::{
    StartSourceUpload, Store,
    artifact::{NasArtifactStore, source_input_path},
};
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};

struct Fixture {
    root: PathBuf,
    artifact_root: PathBuf,
    store: Store,
    pool: SqlitePool,
    artifacts: NasArtifactStore,
    domain_id: DomainId,
}

impl Fixture {
    async fn new() -> Self {
        let root = std::env::temp_dir().join(format!("flori-source-upload-{}", JobId::generate()));
        fs::create_dir(&root).expect("fixture root");
        let database = root.join("flori.sqlite");
        let artifact_root = root.join("artifacts");
        let store = Store::open(&database).await.expect("store");
        let pool = SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("pool");
        let artifacts = NasArtifactStore::new(&artifact_root, 1024 * 1024).expect("NAS");
        let domain_id = DomainId::generate();
        sqlx::query(
            "INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) \
             VALUES(?,?,'PDF','',0,0)",
        )
        .bind(domain_id.to_string())
        .bind(format!("pdf-{domain_id}"))
        .execute(&pool)
        .await
        .expect("domain");
        Self {
            root,
            artifact_root,
            store,
            pool,
            artifacts,
            domain_id,
        }
    }

    fn request(&self, key: &str, bytes: &[u8]) -> CreateUploadSource {
        CreateUploadSource {
            request_key: key.into(),
            kind: SourceKind::PdfUpload,
            title: Some("Paper".into()),
            domain_id: self.domain_id,
            collection_ids: Vec::new(),
            file_sha256: digest(bytes),
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

#[tokio::test]
async fn upload_commits_one_source_input_and_replays_by_request() -> Result<(), sqlx::Error> {
    let fixture = Fixture::new().await;
    let bytes = b"%PDF-1.7\ndigital";
    let request = fixture.request("upload-one", bytes);
    let request_sha = digest(b"canonical-request-one");
    let started = fixture
        .store
        .start_source_upload(
            &fixture.artifacts,
            StartSourceUpload {
                request: &request,
                request_sha256: &request_sha,
                media_type: "application/pdf",
                size_bytes: bytes.len() as u64,
                created_at_ms: 1,
            },
        )
        .await
        .expect("start");
    let upload_id = started.upload_id.expect("new upload");
    fixture
        .store
        .append_source_upload(&fixture.artifacts, upload_id, 0, &digest(bytes), bytes, 2)
        .await
        .expect("append");
    fixture
        .store
        .verify_source_upload(&fixture.artifacts, upload_id, 3)
        .await
        .expect("verify and move");
    let source_id = fixture
        .store
        .commit_source_upload(&fixture.artifacts, upload_id, 4)
        .await
        .expect("commit");
    assert_eq!(source_id, started.source_id);
    let row = sqlx::query(
        "SELECT i.id,i.relative_path,i.sha256,s.request_sha256 FROM source_inputs i \
         JOIN sources s ON s.id=i.source_id WHERE s.id=?",
    )
    .bind(source_id.to_string())
    .fetch_one(&fixture.pool)
    .await
    .expect("source input");
    let input_id = row.try_get::<String, _>("id")?.parse().expect("input ID");
    let expected_path = source_input_path(source_id, input_id, "source.pdf").expect("path");
    assert_eq!(row.try_get::<String, _>("relative_path")?, expected_path);
    assert_eq!(row.try_get::<String, _>("sha256")?, digest(bytes).as_str());
    assert_eq!(
        row.try_get::<String, _>("request_sha256")?,
        request_sha.as_str()
    );
    assert_eq!(
        fs::read(fixture.artifact_root.join(expected_path)).expect("bytes"),
        bytes
    );
    let replay = fixture
        .store
        .start_source_upload(
            &fixture.artifacts,
            StartSourceUpload {
                request: &request,
                request_sha256: &request_sha,
                media_type: "application/pdf",
                size_bytes: bytes.len() as u64,
                created_at_ms: 5,
            },
        )
        .await
        .expect("idempotent replay");
    assert_eq!(replay.source_id, source_id);
    assert_eq!(replay.upload_id, None);
    Ok(())
}

#[tokio::test]
async fn moved_upload_recovers_and_digest_drift_never_creates_source() -> Result<(), sqlx::Error> {
    let fixture = Fixture::new().await;
    let bytes = b"%PDF-1.7\nrecover";
    let request = fixture.request("upload-recover", bytes);
    let request_sha = digest(b"canonical-request-two");
    let started = fixture
        .store
        .start_source_upload(
            &fixture.artifacts,
            StartSourceUpload {
                request: &request,
                request_sha256: &request_sha,
                media_type: "application/pdf",
                size_bytes: bytes.len() as u64,
                created_at_ms: 1,
            },
        )
        .await
        .expect("start");
    let upload_id = started.upload_id.expect("upload");
    fixture
        .store
        .append_source_upload(&fixture.artifacts, upload_id, 0, &digest(bytes), bytes, 2)
        .await
        .expect("append");
    fixture
        .store
        .verify_source_upload(&fixture.artifacts, upload_id, 3)
        .await
        .expect("move");
    fixture
        .store
        .reconcile_uploads(&fixture.artifacts, 4)
        .await
        .expect("startup recovery");
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM sources")
            .fetch_one(&fixture.pool)
            .await?,
        1
    );

    let other = b"%PDF-1.7\nother";
    let bad = fixture.request("upload-bad", b"expected");
    let bad_started = fixture
        .store
        .start_source_upload(
            &fixture.artifacts,
            StartSourceUpload {
                request: &bad,
                request_sha256: &digest(b"bad-request"),
                media_type: "application/pdf",
                size_bytes: other.len() as u64,
                created_at_ms: 5,
            },
        )
        .await
        .expect("bad start");
    let bad_id = bad_started.upload_id.expect("bad upload");
    fixture
        .store
        .append_source_upload(&fixture.artifacts, bad_id, 0, &digest(other), other, 6)
        .await
        .expect("bad bytes accepted before whole digest");
    assert_eq!(
        fixture
            .store
            .verify_source_upload(&fixture.artifacts, bad_id, 7)
            .await
            .unwrap_err()
            .code(),
        ErrorCode::DigestMismatch
    );
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM sources")
            .fetch_one(&fixture.pool)
            .await?,
        1
    );
    Ok(())
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("digest string");
    }
    Sha256Digest::parse(value).expect("SHA-256")
}
