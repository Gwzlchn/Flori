use flori_core::{CredentialKind, ErrorCode, Executor, SecretCredential, SecretInputs, SourceKind};
use sqlx::{Row, Sqlite, Transaction};

use super::super::StoreError;

pub(super) async fn secret_inputs(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    executor: Executor,
) -> Result<SecretInputs, StoreError> {
    if !matches!(
        executor,
        Executor::DocumentAcquire | Executor::VideoAcquire | Executor::VideoSubscription
    ) {
        return Ok(SecretInputs::default());
    }
    let row = sqlx::query(
        "SELECT s.kind AS source_kind,c.kind,c.plaintext_value FROM jobs j \
         JOIN sources s ON s.id=j.source_id \
         LEFT JOIN credentials c ON c.id=s.credential_id WHERE j.id=?",
    )
    .bind(job_id)
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(corrupt)?;
    let kind: Option<String> = row.try_get("kind")?;
    let value: Option<String> = row.try_get("plaintext_value")?;
    match (kind, value) {
        (None, None) => Ok(SecretInputs::default()),
        (Some(kind), Some(value)) => {
            let source = serde_json::from_str::<SourceKind>(&format!(
                "\"{}\"",
                row.try_get::<&str, _>("source_kind")?
            ))
            .map_err(|_| corrupt())?;
            let credential = serde_json::from_str::<CredentialKind>(&format!("\"{kind}\""))
                .map_err(|_| corrupt())?;
            if !credential_matches(source, credential) {
                return Err(StoreError::new(ErrorCode::CredentialUnavailable));
            }
            Ok(SecretInputs {
                credential: Some(SecretCredential {
                    kind: credential,
                    value,
                }),
            })
        }
        _ => Err(corrupt()),
    }
}

fn credential_matches(source: SourceKind, credential: CredentialKind) -> bool {
    matches!(
        (source, credential),
        (
            SourceKind::BilibiliVideo | SourceKind::BilibiliChannel,
            CredentialKind::BilibiliCookie
        ) | (
            SourceKind::YoutubeVideo | SourceKind::YoutubeChannel,
            CredentialKind::YoutubeCookie
        )
    )
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
