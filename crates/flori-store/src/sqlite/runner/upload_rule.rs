use std::path::Path;

use flori_core::{ArtifactDeclaration, ArtifactKind, CompiledTaskSpec, ErrorCode};

use super::super::StoreError;

pub(super) fn declaration<'a>(
    spec: &'a CompiledTaskSpec,
    name: &str,
) -> Result<(&'a ArtifactDeclaration, String), StoreError> {
    for declaration in &spec.artifacts {
        if name == declaration.name && declaration.max_files.is_none() {
            let basename = Path::new(&declaration.path)
                .file_name()
                .and_then(|value| value.to_str())
                .filter(|value| safe_basename(value))
                .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))?;
            return Ok((declaration, basename.to_owned()));
        }
        if let Some(basename) = name
            .strip_prefix(&declaration.name)
            .and_then(|suffix| suffix.strip_prefix('/'))
            && declaration.max_files.is_some()
            && safe_basename(basename)
        {
            return Ok((declaration, basename.to_owned()));
        }
    }
    Err(StoreError::new(ErrorCode::ArtifactUndeclared))
}

fn safe_basename(value: &str) -> bool {
    !value.is_empty() && !value.starts_with('.') && !value.contains(['/', '\\', '\0'])
}

pub(super) const fn retention(kind: ArtifactKind) -> &'static str {
    match kind {
        ArtifactKind::SourceOriginal
        | ArtifactKind::Subtitle
        | ArtifactKind::Danmaku
        | ArtifactKind::PartsManifest
        | ArtifactKind::SubscriptionManifest => "source",
        ArtifactKind::TaskLog | ArtifactKind::AiAudit => "failed_audit",
        ArtifactKind::DocumentStructure
        | ArtifactKind::Figure
        | ArtifactKind::TableRegion
        | ArtifactKind::Translation
        | ArtifactKind::Transcript
        | ArtifactKind::Keyframe
        | ArtifactKind::MechanicalNote
        | ArtifactKind::SmartNote
        | ArtifactKind::Summary
        | ArtifactKind::Terms
        | ArtifactKind::Evidence => "published",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_artifact_kind_has_the_frozen_retention() {
        for kind in [
            ArtifactKind::SourceOriginal,
            ArtifactKind::Subtitle,
            ArtifactKind::Danmaku,
            ArtifactKind::PartsManifest,
            ArtifactKind::SubscriptionManifest,
        ] {
            assert_eq!(retention(kind), "source");
        }
        for kind in [ArtifactKind::TaskLog, ArtifactKind::AiAudit] {
            assert_eq!(retention(kind), "failed_audit");
        }
        for kind in [
            ArtifactKind::DocumentStructure,
            ArtifactKind::Figure,
            ArtifactKind::TableRegion,
            ArtifactKind::Translation,
            ArtifactKind::Transcript,
            ArtifactKind::Keyframe,
            ArtifactKind::MechanicalNote,
            ArtifactKind::SmartNote,
            ArtifactKind::Summary,
            ArtifactKind::Terms,
            ArtifactKind::Evidence,
        ] {
            assert_eq!(retention(kind), "published");
        }
    }
}
