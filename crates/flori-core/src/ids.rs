use std::{fmt, str::FromStr};

use serde::{Deserialize, Deserializer, Serialize, Serializer, de};
use utoipa::ToSchema;
use uuid::{Uuid, Version};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct IdParseError;

impl fmt::Display for IdParseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("expected a canonical lowercase UUIDv7")
    }
}

impl std::error::Error for IdParseError {}

fn parse_uuid_v7(value: &str) -> Result<Uuid, IdParseError> {
    let uuid = Uuid::parse_str(value).map_err(|_| IdParseError)?;
    if uuid.get_version() != Some(Version::SortRand) || uuid.hyphenated().to_string() != value {
        return Err(IdParseError);
    }
    Ok(uuid)
}

macro_rules! define_ids {
    ($($name:ident),+ $(,)?) => {
        $(
            #[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd, ToSchema)]
            #[schema(
                value_type = String,
                pattern = "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            )]
            pub struct $name(Uuid);

            impl $name {
                #[must_use]
                pub fn generate() -> Self {
                    Self(Uuid::now_v7())
                }
            }

            impl FromStr for $name {
                type Err = IdParseError;

                fn from_str(value: &str) -> Result<Self, Self::Err> {
                    parse_uuid_v7(value).map(Self)
                }
            }

            impl fmt::Display for $name {
                fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                    write!(formatter, "{}", self.0.hyphenated())
                }
            }

            impl Serialize for $name {
                fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
                where
                    S: Serializer,
                {
                    serializer.collect_str(&self.0.hyphenated())
                }
            }

            impl<'de> Deserialize<'de> for $name {
                fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
                where
                    D: Deserializer<'de>,
                {
                    let value = <&str>::deserialize(deserializer)?;
                    value.parse().map_err(de::Error::custom)
                }
            }
        )+
    };
}

define_ids!(
    PipelineId,
    PipelineRevisionId,
    SourceId,
    SourceInputId,
    JobId,
    TaskId,
    AttemptId,
    ArtifactId,
    RunnerId,
    PromptSnapshotId,
    UploadId,
    CredentialId,
    AiUsageId,
    DomainId,
    CollectionId,
    GlossaryTermId,
    ConceptOccurrenceId,
    EvidenceId,
    SearchChunkId,
    QrSessionId,
    RequestId,
);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uuid_v7_round_trips_through_json() {
        let id = JobId::generate();
        let encoded = serde_json::to_string(&id).expect("serialize UUIDv7");
        let decoded: JobId = serde_json::from_str(&encoded).expect("deserialize UUIDv7");

        assert_eq!(decoded, id);
        assert_eq!(encoded, format!("\"{id}\""));
    }

    #[test]
    fn rejects_other_uuid_versions() {
        let v4 = "550e8400-e29b-41d4-a716-446655440000";
        let error = serde_json::from_str::<SourceId>(&format!("\"{v4}\""))
            .expect_err("UUIDv4 must be rejected");

        assert!(error.to_string().contains("UUIDv7"));
    }

    #[test]
    fn rejects_noncanonical_uuid_v7() {
        let uppercase = JobId::generate().to_string().to_uppercase();
        serde_json::from_str::<JobId>(&format!("\"{uppercase}\""))
            .expect_err("uppercase UUIDv7 must be rejected");
    }
}
