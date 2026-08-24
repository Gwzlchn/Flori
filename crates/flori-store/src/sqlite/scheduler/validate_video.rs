use flori_core::{
    ArtifactKind, ErrorCode, JobId, TermsManifest, TranscriptManifest, VideoKeyframe,
    validate_video_evidence,
};

use crate::artifact::NasArtifactStore;

use super::{
    super::{Store, StoreError},
    validate::{one, read_text},
};

impl Store {
    pub(super) async fn video_evidence_bytes(
        &self,
        artifacts: &NasArtifactStore,
        job_id: JobId,
    ) -> Result<Vec<u8>, StoreError> {
        let inputs = self.validation_inputs(job_id).await?;
        let transcript: TranscriptManifest = serde_json::from_str(&read_text(
            artifacts,
            one(&inputs, ArtifactKind::Transcript)?,
        )?)
        .map_err(|_| invalid())?;
        let smart_note = read_text(artifacts, one(&inputs, ArtifactKind::SmartNote)?)?;
        let summary = read_text(artifacts, one(&inputs, ArtifactKind::Summary)?)?;
        let terms: TermsManifest =
            serde_json::from_str(&read_text(artifacts, one(&inputs, ArtifactKind::Terms)?)?)
                .map_err(|_| invalid())?;
        let original = one(&inputs, ArtifactKind::SourceOriginal)?;
        if transcript.source_artifact_id != original.id {
            return Err(invalid());
        }
        let keyframes = inputs
            .iter()
            .filter(|item| item.kind == ArtifactKind::Keyframe)
            .map(|item| {
                VideoKeyframe::from_artifact_name(item.id, &item.name).map_err(|_| invalid())
            })
            .collect::<Result<Vec<_>, _>>()?;
        if keyframes.is_empty() {
            return Err(invalid());
        }
        let manifest =
            validate_video_evidence(&transcript, &keyframes, 1, &terms, &smart_note, &summary)
                .map_err(StoreError::new)?;
        serde_json::to_vec(&manifest).map_err(|_| StoreError::new(ErrorCode::Internal))
    }
}

fn invalid() -> StoreError {
    StoreError::new(ErrorCode::EvidenceInvalid)
}
