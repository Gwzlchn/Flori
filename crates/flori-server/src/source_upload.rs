use std::{
    fmt::Write as _,
    path::{Path, PathBuf},
};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Multipart, State},
    routing::post,
};
use flori_core::{
    CreateUploadSource, CreatedSource, ErrorCode, RequestId, Sha256Digest, SourceKind,
};
use flori_store::StartSourceUpload;
use sha2::{Digest, Sha256};
use tokio::{
    fs::File,
    io::{AsyncReadExt, AsyncSeekExt, AsyncWriteExt},
};

use crate::{error::HttpError, runner::HttpState};

const MAX_UPLOAD_BYTES: u64 = 128 * 1024 * 1024;
const CHUNK_BYTES: usize = 1024 * 1024;
const METADATA_BYTES: usize = 64 * 1024;

pub(super) fn routes() -> Router<HttpState> {
    Router::new().route(
        "/api/v1/sources/uploads",
        post(upload).layer(DefaultBodyLimit::max(
            usize::try_from(MAX_UPLOAD_BYTES).expect("upload limit fits usize") + METADATA_BYTES,
        )),
    )
}

async fn upload(
    State(state): State<HttpState>,
    mut multipart: Multipart,
) -> Result<Json<CreatedSource>, HttpError> {
    let path = std::env::temp_dir().join(format!("flori-upload-{}", RequestId::generate()));
    let temporary = TemporaryUpload(path);
    let mut metadata = None;
    let mut media_type = None;
    let mut size_bytes = None;
    while let Some(mut field) = multipart
        .next_field()
        .await
        .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?
    {
        match field.name() {
            Some("metadata") if metadata.is_none() => {
                let bytes = field
                    .bytes()
                    .await
                    .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
                if bytes.len() > METADATA_BYTES {
                    return Err(HttpError::payload_too_large());
                }
                metadata = Some(
                    serde_json::from_slice::<CreateUploadSource>(&bytes)
                        .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?,
                );
            }
            Some("file") if size_bytes.is_none() => {
                media_type = field.content_type().map(ToOwned::to_owned);
                let mut output = File::create(&temporary.0)
                    .await
                    .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
                let mut size = 0_u64;
                while let Some(chunk) = field
                    .chunk()
                    .await
                    .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?
                {
                    size = size
                        .checked_add(chunk.len() as u64)
                        .filter(|value| *value <= MAX_UPLOAD_BYTES)
                        .ok_or_else(HttpError::payload_too_large)?;
                    output
                        .write_all(&chunk)
                        .await
                        .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
                }
                output
                    .sync_all()
                    .await
                    .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
                size_bytes = Some(size);
            }
            _ => return Err(HttpError::new(ErrorCode::InvalidRequest)),
        }
    }
    let request = metadata.ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))?;
    let size_bytes = size_bytes
        .filter(|size| *size != 0)
        .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))?;
    let media_type = media_type.ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))?;
    validate_file(&temporary.0, &request, &media_type, size_bytes).await?;
    let request_sha256 =
        digest(&serde_json::to_vec(&request).map_err(|_| HttpError::new(ErrorCode::Internal))?);
    let prepared = state
        .store
        .start_source_upload(
            &state.artifacts,
            StartSourceUpload {
                request: &request,
                request_sha256: &request_sha256,
                media_type: &media_type,
                size_bytes,
                created_at_ms: super::runner::now_ms()?,
            },
        )
        .await?;
    if let Some(upload_id) = prepared.upload_id {
        copy_to_ledger(&state, &temporary.0, upload_id, prepared.received_bytes).await?;
        let now = super::runner::now_ms()?;
        state
            .store
            .verify_source_upload(&state.artifacts, upload_id, now)
            .await?;
        state
            .store
            .commit_source_upload(&state.artifacts, upload_id, now)
            .await?;
    }
    Ok(Json(CreatedSource {
        source_id: prepared.source_id,
    }))
}

async fn copy_to_ledger(
    state: &HttpState,
    path: &Path,
    upload_id: flori_core::UploadId,
    mut offset: u64,
) -> Result<(), HttpError> {
    let mut file = File::open(path)
        .await
        .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
    file.seek(std::io::SeekFrom::Start(offset))
        .await
        .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    loop {
        let read = file
            .read(&mut buffer)
            .await
            .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
        if read == 0 {
            return Ok(());
        }
        let now = super::runner::now_ms()?;
        let cursor = state
            .store
            .append_source_upload(
                &state.artifacts,
                upload_id,
                offset,
                &digest(&buffer[..read]),
                &buffer[..read],
                now,
            )
            .await?;
        offset = cursor.received_bytes;
    }
}

async fn validate_file(
    path: &Path,
    request: &CreateUploadSource,
    media_type: &str,
    size_bytes: u64,
) -> Result<(), HttpError> {
    let expected = match request.kind {
        SourceKind::PdfUpload => ("application/pdf", b"%PDF-".as_slice(), 0),
        SourceKind::LocalVideo => ("video/mp4", b"ftyp".as_slice(), 4),
        _ => return Err(HttpError::new(ErrorCode::UnsupportedSource)),
    };
    if media_type != expected.0 || size_bytes < (expected.2 + expected.1.len()) as u64 {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    let mut file = File::open(path)
        .await
        .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    let mut prefix = Vec::with_capacity(8);
    let mut actual_size = 0_u64;
    loop {
        let read = file
            .read(&mut buffer)
            .await
            .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
        if read == 0 {
            break;
        }
        actual_size += read as u64;
        hasher.update(&buffer[..read]);
        let needed = 8_usize.saturating_sub(prefix.len()).min(read);
        prefix.extend_from_slice(&buffer[..needed]);
    }
    if actual_size != size_bytes
        || !prefix[expected.2..].starts_with(expected.1)
        || digest_bytes(hasher.finalize().as_slice()) != request.file_sha256
    {
        return Err(HttpError::new(ErrorCode::DigestMismatch));
    }
    Ok(())
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    digest_bytes(Sha256::digest(bytes).as_slice())
}

fn digest_bytes(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in bytes {
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(value).expect("SHA-256 formatter is canonical")
}

struct TemporaryUpload(PathBuf);

impl Drop for TemporaryUpload {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}
