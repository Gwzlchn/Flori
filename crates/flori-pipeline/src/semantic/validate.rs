use super::super::*;

pub(super) fn validate_core_input(
    field: &str,
    producer: &CompiledTask,
) -> Result<(), CompileError> {
    let expected: &[(ArtifactKind, bool)] = match field {
        "source" if producer.executor == Executor::DocumentExtract => &[
            (ArtifactKind::DocumentStructure, true),
            (ArtifactKind::Figure, false),
            (ArtifactKind::TableRegion, false),
        ],
        "notes" if producer.executor == Executor::AiDocumentNote => &[
            (ArtifactKind::SmartNote, true),
            (ArtifactKind::Summary, true),
            (ArtifactKind::Terms, true),
        ],
        _ => return Err(invalid()),
    };
    expected
        .iter()
        .all(|(kind, required)| {
            producer
                .artifacts
                .iter()
                .filter(|artifact| artifact.kind == *kind && artifact.required == *required)
                .count()
                == 1
        })
        .then_some(())
        .ok_or_else(invalid)
}
