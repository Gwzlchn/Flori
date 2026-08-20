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
    let content_artifacts = producer
        .artifacts
        .iter()
        .filter(|artifact| !matches!(artifact.kind, ArtifactKind::TaskLog | ArtifactKind::AiAudit));
    (content_artifacts.count() == expected.len()
        && expected.iter().all(|(kind, required)| {
            let mut matching = producer
                .artifacts
                .iter()
                .filter(|artifact| artifact.kind == *kind);
            matching
                .next()
                .is_some_and(|artifact| artifact.required == *required)
                && matching.next().is_none()
        }))
    .then_some(())
    .ok_or_else(invalid)
}
