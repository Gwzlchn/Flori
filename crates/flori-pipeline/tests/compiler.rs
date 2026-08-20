use flori_core::ErrorCode;
use flori_pipeline::compile;

const PDF: &str = include_str!("../../../pipelines/pdf.yml");

#[test]
fn compiles_pdf_deterministically() {
    let first = compile("pdf", PDF.as_bytes()).expect("valid PDF pipeline");
    let second = compile("pdf", PDF.as_bytes()).expect("same pipeline remains valid");

    assert_eq!(first, second);
    assert_eq!(
        first.canonical_json,
        serde_json::to_string(&first.pipeline).unwrap()
    );
    assert_eq!(first.sha256.len(), 64);
    assert_eq!(
        first.pipeline.topological_order,
        [
            "acquire",
            "extract",
            "note",
            "translate",
            "validate",
            "publish"
        ]
    );
    for task in first.pipeline.tasks.values() {
        let (spec, bindings) = task.freeze_for_job().expect("freeze compiled task");
        assert_eq!(spec.executor, task.executor);
        assert_eq!(bindings.executor(), task.executor);
        assert!(bindings.is_valid());
    }
}

#[test]
fn rejects_unknown_duplicate_and_forbidden_yaml() {
    let acquire = PDF.split_once("\n\nextract:").unwrap().0;
    let duplicate_task = format!("{PDF}\n{acquire}\n");
    for yaml in [
        PDF.replacen(
            "  executor: document.acquire",
            "  executor: document.acquire\n  script: nope",
            1,
        ),
        PDF.replacen(
            "  executor: document.acquire",
            "  executor: document.acquire\n  executor: document.acquire",
            1,
        ),
        PDF.replacen("  tags: [media]", "  tags: &shared [media]", 1),
        PDF.replacen("  tags: [media]", "  tags: *shared", 1),
        PDF.replacen(
            "  executor: document.acquire",
            "  executor: !custom document.acquire",
            1,
        ),
        PDF.replacen(
            "  executor: document.acquire",
            "  executor: document.acquire\n  <<: {}",
            1,
        ),
        PDF.replacen("    source: $source", "    source: {nested: bad}", 1),
        PDF.replacen("    source: $source", "    source: !custom $source", 1),
        PDF.replacen("    source: $source", "    source: null", 1),
        duplicate_task,
    ] {
        assert!(
            compile("pdf", yaml.as_bytes()).is_err(),
            "accepted:\n{yaml}"
        );
    }
    assert_eq!(
        compile("pdf", &[0xff]).unwrap_err().code(),
        ErrorCode::PipelineInvalid
    );
}

#[test]
fn rejects_graph_executor_artifact_and_reference_attacks() {
    let cycle = PDF.replacen(
        "  with:\n    source: $source",
        "  with:\n    source: $source\n  needs: [publish]",
        1,
    );
    assert_eq!(
        compile("pdf", cycle.as_bytes()).unwrap_err().code(),
        ErrorCode::PipelineCycle
    );

    for yaml in [
        PDF.replacen("needs: [acquire]", "needs: [missing]", 1),
        PDF.replacen("document.acquire", "shell.run", 1),
        PDF.replacen("kind: source_original", "kind: summary", 1),
        PDF.replacen("$needs.acquire.original", "$needs.acquire.log", 1),
        PDF.replacen("$needs.extract.structure", "$needs.extract.tables", 1),
        PDF.replacen("path: output/source.pdf", "path: ../source.pdf", 1),
        PDF.replacen("retry: 1", "retry: 3", 1),
        PDF.replacen("timeout: 10m", "timeout: 0s", 1),
        PDF.replacen("tags: [media]", "tags: [Media]", 1),
        PDF.replacen("name: original", "name: Original", 1),
        PDF.replacen("publish:", "Publish:", 1),
        PDF.replacen("  needs: [translate, validate]", "  needs: []", 1),
    ] {
        assert!(
            compile("pdf", yaml.as_bytes()).is_err(),
            "accepted:\n{yaml}"
        );
    }
}

#[test]
fn rejects_conditional_output_with_different_consumer_rules() {
    let yaml = PDF.replacen(
        "  needs: [acquire]\n  tags: [media]",
        "  needs: [acquire]\n  rules:\n    - if: $job.translate == true\n  tags: [media]",
        1,
    );

    assert!(compile("pdf", yaml.as_bytes()).is_err());
}

#[test]
fn rejects_missing_executor_inputs() {
    for yaml in [
        PDF.replacen(
            "  with:\n    source: $source\n  tags:",
            "  with: {}\n  tags:",
            1,
        ),
        PDF.replacen(
            "  with:\n    pdf: $needs.acquire.original\n  needs:",
            "  with: {}\n  needs:",
            1,
        ),
        PDF.replacen(
            "    document: $needs.extract.structure\n    prompt: $prompts.document_translate",
            "    prompt: $prompts.document_translate",
            1,
        ),
        PDF.replacen(
            "    document: $needs.extract.structure\n    prompt: $prompts.document_translate",
            "    document: $needs.extract.structure",
            1,
        ),
        PDF.replacen(
            "    document: $needs.extract.structure\n    prompt: $prompts.document_note",
            "    prompt: $prompts.document_note",
            1,
        ),
        PDF.replacen(
            "    document: $needs.extract.structure\n    prompt: $prompts.document_note",
            "    document: $needs.extract.structure",
            1,
        ),
        PDF.replacen(
            "    source: $needs.extract\n    notes: $needs.note",
            "    notes: $needs.note",
            1,
        ),
        PDF.replacen(
            "    source: $needs.extract\n    notes: $needs.note",
            "    source: $needs.extract",
            1,
        ),
        PDF.replacen(
            "  with:\n    validated: $needs.validate.evidence",
            "  with: {}",
            1,
        ),
    ] {
        assert!(
            compile("pdf", yaml.as_bytes()).is_err(),
            "accepted:\n{yaml}"
        );
    }
}

#[test]
fn rejects_wrong_input_shapes_and_duplicate_keys() {
    for yaml in [
        PDF.replacen("pdf: $needs.acquire.original", "pdf: hello", 1),
        PDF.replacen(
            "pdf: $needs.acquire.original",
            "pdf: [$needs.acquire.original]",
            1,
        ),
        PDF.replacen("pdf: $needs.acquire.original", "pdf: $job.translate", 1),
        PDF.replacen("pdf: $needs.acquire.original", "pdf: $needs.acquire", 1),
        PDF.replacen(
            "prompt: $prompts.document_note",
            "prompt: $domain.profile",
            1,
        ),
        PDF.replacen("notes: $needs.note", "notes: $needs.note.smart_note", 1),
        PDF.replacen("source: $needs.extract", "source: $needs.acquire", 1),
        PDF.replacen("notes: $needs.note", "notes: $needs.extract", 1),
        PDF.replacen(
            "name: structure, kind: document_structure, path: output/document.json, required: true",
            "name: structure, kind: document_structure, path: output/document.json, required: false",
            1,
        ),
        PDF.replacen(
            "    - { name: terms, kind: terms, path: output/terms.json, required: true, when: on_success, max_bytes: 10485760 }\n",
            "",
            1,
        ),
        PDF.replacen(
            "    - { name: figures, kind: figure, path: output/figures/*, required: false, when: on_success, max_files: 128, max_bytes: 20971520 }",
            "    - { name: figures, kind: figure, path: output/figures/*, required: false, when: on_success, max_files: 128, max_bytes: 20971520 }\n    - { name: shadow_structure, kind: document_structure, path: output/shadow.json, required: false, when: on_success, max_bytes: 10485760 }",
            1,
        ),
        PDF.replacen(
            "    - { name: terms, kind: terms, path: output/terms.json, required: true, when: on_success, max_bytes: 10485760 }",
            "    - { name: terms, kind: terms, path: output/terms.json, required: true, when: on_success, max_bytes: 10485760 }\n    - { name: shadow_note, kind: smart_note, path: output/shadow.md, required: false, when: on_success, max_bytes: 10485760 }",
            1,
        ),
        PDF.replacen(
            "    pdf: $needs.acquire.original",
            "    pdf: $needs.acquire.original\n    pdf: $needs.acquire.original",
            1,
        ),
    ] {
        assert_eq!(
            compile("pdf", yaml.as_bytes()).unwrap_err().code(),
            ErrorCode::PipelineInvalid,
            "accepted:\n{yaml}"
        );
    }
}
