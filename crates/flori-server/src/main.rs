//! Home Core 进程入口。

#![forbid(unsafe_code)]

use std::{
    env, fs, io,
    net::SocketAddr,
    path::Path,
    process::ExitCode,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use flori_store::{Store, artifact::NasArtifactStore};
use tokio::net::TcpListener;

const USAGE: &str = "usage:\n  flori-server export-openapi <output>\n  flori-server serve <listen> <sqlite> <artifact-root> <artifact-download-base> <max-artifact-bytes> <lease-ms>";

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("flori-server failed: {error}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = env::args_os().skip(1).collect::<Vec<_>>();
    match args.as_slice() {
        [command, output] if command == "export-openapi" => export_openapi(Path::new(output)),
        [
            command,
            listen,
            sqlite,
            artifact_root,
            download_base,
            max_bytes,
            lease_ms,
        ] if command == "serve" => {
            serve(
                parse_text(listen)?.parse()?,
                Path::new(sqlite),
                Path::new(artifact_root),
                parse_text(download_base)?.to_owned(),
                parse_positive(max_bytes)?,
                parse_positive(lease_ms)?,
            )
            .await
        }
        _ => Err(io::Error::new(io::ErrorKind::InvalidInput, USAGE).into()),
    }
}

async fn serve(
    listen: SocketAddr,
    sqlite: &Path,
    artifact_root: &Path,
    artifact_download_base: String,
    max_artifact_bytes: u64,
    lease_ms: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    let store = Arc::new(Store::open(sqlite).await?);
    let artifacts = Arc::new(NasArtifactStore::new(artifact_root, max_artifact_bytes)?);
    store.reconcile_uploads(&artifacts, now_ms()?).await?;
    let app = flori_server::app(store, artifacts, artifact_download_base, lease_ms)
        .map_err(|code| io::Error::new(io::ErrorKind::InvalidInput, format!("{code:?}")))?;
    let listener = TcpListener::bind(listen).await?;
    axum::serve(listener, app).await?;
    Ok(())
}

fn export_openapi(output: &Path) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(output, flori_core::openapi_json()?)?;
    Ok(())
}

fn parse_text(value: &std::ffi::OsStr) -> Result<&str, io::Error> {
    value
        .to_str()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, USAGE))
}

fn parse_positive(value: &std::ffi::OsStr) -> Result<u64, io::Error> {
    parse_text(value)?
        .parse()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, USAGE))
}

fn now_ms() -> Result<i64, Box<dyn std::error::Error>> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)?
        .as_millis()
        .try_into()?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn recovery_failure_happens_before_listen() {
        let root = env::temp_dir().join(format!(
            "flori-server-startup-{}",
            flori_core::RequestId::generate()
        ));
        fs::create_dir(&root).expect("test root");
        let database = root.join("flori.sqlite");
        Store::open(&database).await.expect("schema");
        let pool = sqlx::SqlitePool::connect_with(
            sqlx::sqlite::SqliteConnectOptions::new()
                .filename(&database)
                .foreign_keys(true),
        )
        .await
        .expect("fixture pool");
        let upload_id = flori_core::UploadId::generate();
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,request_key,request_sha256,commit_json, \
             name,target_id,staging_path,final_relative_path,expected_size_bytes,expected_sha256, \
             received_bytes,state,created_at_ms,updated_at_ms) VALUES(?,'source',?,?,?,'{}',?,?, \
             ?,?,0,?,0,'receiving',0,0)",
        )
        .bind(upload_id.to_string())
        .bind(flori_core::SourceId::generate().to_string())
        .bind("source-upload")
        .bind("a".repeat(64))
        .bind("input")
        .bind(flori_core::SourceInputId::generate().to_string())
        .bind(format!(".staging/uploads/{upload_id}"))
        .bind("sources/invalid/input.pdf")
        .bind("b".repeat(64))
        .execute(&pool)
        .await
        .expect("unsupported source ledger");
        pool.close().await;

        let reservation = TcpListener::bind("localhost:0")
            .await
            .expect("reserve port");
        let address = reservation.local_addr().expect("address");
        drop(reservation);
        assert!(
            serve(
                address,
                &database,
                &root.join("artifacts"),
                "http://localhost/content".to_owned(),
                1024,
                60_000,
            )
            .await
            .is_err()
        );
        let listener = TcpListener::bind(address)
            .await
            .expect("recovery failed before binding");
        drop(listener);
        fs::remove_dir_all(root).expect("remove fixture");
    }
}
