use std::path::Path;

use flori_core::{
    ArtifactDeclaration, ArtifactManifestEntry, ErrorCode, StartUploadRequest, TaskClaim,
    VerifyUploadRequest,
};
use sha2::{Digest, Sha256};
use tokio::{
    fs,
    io::{AsyncReadExt, AsyncSeekExt},
};

use crate::RunnerClient;

const CHUNK_BYTES: usize = 1024 * 1024;

pub(super) async fn file(
    client: &RunnerClient,
    claim: &TaskClaim,
    declaration: &ArtifactDeclaration,
    name: String,
    media_type: &str,
    path: &Path,
) -> Result<ArtifactManifestEntry, ErrorCode> {
    let metadata = fs::symlink_metadata(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() == 0 {
        return Err(ErrorCode::ArtifactInvalidPath);
    }
    if metadata.len() > declaration.max_bytes || !declaration.kind.accepts_media_type(media_type) {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    let sha256 = digest(path).await?;
    let request = StartUploadRequest {
        name,
        media_type: media_type.to_owned(),
        size_bytes: metadata.len(),
        sha256: sha256.clone(),
    };
    let started = client
        .start_upload(claim.exec_id, &request)
        .await
        .map_err(|error| error.code())?;
    validate_entry(&started.artifact, declaration, &request)?;
    if started.received_bytes > request.size_bytes {
        return Err(ErrorCode::CorruptState);
    }
    let mut input = fs::File::open(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    input
        .seek(std::io::SeekFrom::Start(started.received_bytes))
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let mut offset = started.received_bytes;
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    while offset < request.size_bytes {
        let remaining = usize::try_from(request.size_bytes - offset)
            .unwrap_or(usize::MAX)
            .min(CHUNK_BYTES);
        let count = input
            .read(&mut buffer[..remaining])
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        if count == 0 {
            return Err(ErrorCode::DigestMismatch);
        }
        let next = offset
            .checked_add(u64::try_from(count).map_err(|_| ErrorCode::ArtifactTooLarge)?)
            .ok_or(ErrorCode::ArtifactTooLarge)?;
        let cursor = client
            .append_upload_chunk(started.upload_id, offset, buffer[..count].to_vec())
            .await
            .map_err(|error| error.code())?;
        if cursor.upload_id != started.upload_id || cursor.received_bytes != next {
            return Err(ErrorCode::CorruptState);
        }
        offset = next;
    }
    let verified = client
        .verify_upload(
            started.upload_id,
            &VerifyUploadRequest {
                size_bytes: request.size_bytes,
                sha256,
            },
        )
        .await
        .map_err(|error| error.code())?;
    if verified.upload_id != started.upload_id || verified.artifact != started.artifact {
        return Err(ErrorCode::CorruptState);
    }
    Ok(verified.artifact)
}

fn validate_entry(
    entry: &ArtifactManifestEntry,
    declaration: &ArtifactDeclaration,
    request: &StartUploadRequest,
) -> Result<(), ErrorCode> {
    if entry.name != request.name
        || entry.kind != declaration.kind
        || entry.media_type != request.media_type
        || entry.size_bytes != request.size_bytes
        || entry.sha256 != request.sha256
        || entry.relative_path.is_empty()
    {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}

async fn digest(path: &Path) -> Result<flori_core::Sha256Digest, ErrorCode> {
    let mut input = fs::File::open(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0_u8; CHUNK_BYTES];
    loop {
        let count = input
            .read(&mut buffer)
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    flori_core::Sha256Digest::parse(
        digest
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| ErrorCode::Internal)
}
