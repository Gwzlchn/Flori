use flori_core::{AiModelCapability, AiResultEnvelope, Executor, UsageOrigin, UsageUpdate};
use flori_runner::{
    QODERCLI_PROGRAM, QODERCLI_VERSION, QoderError, qoder_invocation_command as invocation_command,
    qoder_model_list_command as model_list_command, qoder_parse_result as parse_result,
    qoder_verify_model_allowlist as verify_model_allowlist, qoder_verify_version as verify_version,
    qoder_version_command as version_command,
};

const DOCUMENT_NOTE: &str = r#"{"executor":"ai.document_note","schema":"flori.ai_result.v1","smart_note_markdown":"note","summary_markdown":"summary","terms":{"schema":"flori.terms.v1","terms":[],"evidence_candidates":[]}}"#;
const TRANSLATION: &str = r#"{"executor":"ai.document_translate","schema":"flori.ai_result.v1","translation_markdown":"translated"}"#;

fn result_output(envelope: &str, credits: &str) -> Vec<u8> {
    let escaped = serde_json::to_string(envelope).expect("escape fixture envelope");
    format!(
        r#"{{"type":"result","subtype":"success","duration_ms":15,"duration_api_ms":12,"is_error":false,"num_turns":1,"result":{escaped},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":{credits},"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake-uuid","session_id":"fake-session"}}"#,
    )
    .into_bytes()
}

fn parse_note(output: &[u8]) -> Result<flori_runner::QoderResult, QoderError> {
    parse_result(
        Some(0),
        output,
        1024 * 1024,
        Executor::AiDocumentNote,
        "attempt-1:qoder".to_owned(),
    )
}

#[test]
fn commands_are_fixed_and_keep_prompt_and_secrets_out_of_audit_arguments() {
    assert_eq!(QODERCLI_PROGRAM, "qodercli");
    assert_eq!(version_command().arguments(), ["--version"]);
    assert_eq!(model_list_command().arguments(), ["--list-models"]);

    let prompt = "private source text";
    let credential_marker = "credential-must-not-leak";
    let note = invocation_command(
        Executor::AiDocumentNote,
        "Ultimate",
        "high",
        "/task",
        prompt.to_owned(),
    )
    .expect("note command");
    assert_eq!(note.standard_input(), Some(prompt));
    assert_eq!(
        note.arguments().last().map(String::as_str),
        Some("WebSearch")
    );
    assert!(note.arguments().iter().all(|arg| !arg.contains(prompt)));
    assert!(
        note.arguments()
            .iter()
            .all(|arg| !arg.contains(credential_marker))
    );
    assert!(
        note.redacted_arguments()
            .iter()
            .all(|arg| !arg.contains(prompt) && !arg.contains(credential_marker))
    );

    let translation = invocation_command(
        Executor::AiDocumentTranslate,
        "Ultimate",
        "high",
        "/task",
        "translate".to_owned(),
    )
    .expect("translation command");
    assert_eq!(translation.arguments().last().map(String::as_str), Some(""));
    assert_eq!(
        invocation_command(
            Executor::DocumentExtract,
            "Ultimate",
            "high",
            "/task",
            "wrong executor".to_owned(),
        )
        .err(),
        Some(QoderError::InvalidCommand)
    );
}

#[test]
fn probes_require_the_locked_version_and_explicit_model_allowlist() {
    verify_version(format!("{QODERCLI_VERSION}\n").as_bytes()).expect("locked version");
    assert_eq!(verify_version(b"1.1.25\n"), Err(QoderError::InvalidVersion));
    assert_eq!(
        verify_version(format!("{QODERCLI_VERSION}\n\n").as_bytes()),
        Err(QoderError::InvalidVersion)
    );

    let models = b"MODEL\nAuto\nUltimate\nPeach (custom-model-id)\n";
    let allowlist = vec![
        AiModelCapability {
            model: "Auto".to_owned(),
            efforts: vec!["low".to_owned(), "high".to_owned()],
        },
        AiModelCapability {
            model: "Ultimate".to_owned(),
            efforts: vec!["high".to_owned()],
        },
    ];
    verify_model_allowlist(models, &allowlist).expect("configured display names are present");

    let unavailable = [AiModelCapability {
        model: "Missing".to_owned(),
        efforts: vec!["high".to_owned()],
    }];
    assert_eq!(
        verify_model_allowlist(models, &unavailable),
        Err(QoderError::ModelUnavailable)
    );
    let custom = [AiModelCapability {
        model: "Peach (custom-model-id)".to_owned(),
        efforts: vec!["high".to_owned()],
    }];
    assert_eq!(
        verify_model_allowlist(models, &custom),
        Err(QoderError::InvalidModelList)
    );
    let invalid_efforts = [AiModelCapability {
        model: "Auto".to_owned(),
        efforts: vec!["high".to_owned(), "high".to_owned()],
    }];
    assert_eq!(
        verify_model_allowlist(models, &invalid_efforts),
        Err(QoderError::InvalidModelList)
    );
}

#[test]
fn result_decodes_the_strict_envelope_and_only_reports_credits() {
    let parsed = parse_note(&result_output(DOCUMENT_NOTE, "2.500001")).expect("valid result");
    assert!(matches!(
        parsed.envelope,
        AiResultEnvelope::DocumentNote { .. }
    ));
    assert_eq!(
        parsed.usage,
        UsageUpdate::Final {
            invocation_key: "attempt-1:qoder".to_owned(),
            origin: UsageOrigin::Observed,
            input_tokens: None,
            output_tokens: None,
            cost_micros: None,
            credits_micros: Some(2_500_001),
        }
    );

    let decimal_locator = format!(
        r#"{{"executor":"ai.document_note","schema":"flori.ai_result.v1","smart_note_markdown":"note","summary_markdown":"summary","terms":{}}}"#,
        include_str!("../../../tests/fixtures/vnext/expected/terms.json")
    );
    parse_note(&result_output(&decimal_locator, "0.000001"))
        .expect("PDF decimal locator and exact credits coexist");
}

#[test]
fn credits_are_exact_micros_and_never_fabricated() {
    for (credits, expected) in [
        ("0", 0),
        ("0.000001", 1),
        ("1e-6", 1),
        ("2.5", 2_500_000),
        ("12.345678", 12_345_678),
        ("18446744073709.551615", u64::MAX),
    ] {
        let parsed = parse_note(&result_output(DOCUMENT_NOTE, credits)).expect("exact credits");
        assert!(matches!(
            parsed.usage,
            UsageUpdate::Final {
                credits_micros: Some(value),
                ..
            } if value == expected
        ));
    }

    for credits in ["-1", "0.0000001", "18446744073710"] {
        assert_eq!(
            parse_note(&result_output(DOCUMENT_NOTE, credits)).err(),
            Some(QoderError::InvalidCredits),
            "credits={credits}"
        );
    }
    let missing = result_output(DOCUMENT_NOTE, "1");
    let missing = String::from_utf8(missing)
        .expect("fixture UTF-8")
        .replace(",\"total_credits\":1", "");
    assert_eq!(
        parse_note(missing.as_bytes()).err(),
        Some(QoderError::InvalidOutput)
    );
}

#[test]
fn malformed_duplicate_unknown_and_oversize_outputs_fail_closed() {
    let unknown_inner = format!(
        "{},\"extra\":true}}",
        DOCUMENT_NOTE.strip_suffix('}').expect("object fixture")
    );
    assert_eq!(
        parse_note(&result_output(&unknown_inner, "1")).err(),
        Some(QoderError::InvalidOutput)
    );
    let duplicate_inner = DOCUMENT_NOTE.replace(
        "\"smart_note_markdown\":\"note\"",
        "\"smart_note_markdown\":\"note\",\"smart_note_markdown\":\"again\"",
    );
    assert_eq!(
        parse_note(&result_output(&duplicate_inner, "1")).err(),
        Some(QoderError::InvalidOutput)
    );

    let output = result_output(DOCUMENT_NOTE, "1");
    let unknown_outer = String::from_utf8(output.clone())
        .expect("fixture UTF-8")
        .replace("{\"type\"", "{\"unexpected\":1,\"type\"");
    assert_eq!(
        parse_note(unknown_outer.as_bytes()).err(),
        Some(QoderError::InvalidOutput)
    );
    let duplicate_outer = String::from_utf8(output.clone())
        .expect("fixture UTF-8")
        .replace(
            "\"total_credits\":1",
            "\"total_credits\":1,\"total_credits\":2",
        );
    assert_eq!(
        parse_note(duplicate_outer.as_bytes()).err(),
        Some(QoderError::InvalidOutput)
    );
    assert_eq!(
        parse_result(
            Some(0),
            &output,
            output.len() - 1,
            Executor::AiDocumentNote,
            "attempt-1:qoder".to_owned(),
        )
        .err(),
        Some(QoderError::OutputTooLarge)
    );
}

#[test]
fn exit_failure_and_wrong_executor_fail_without_fallback() {
    assert_eq!(
        parse_note(b"not json after failure").err(),
        Some(QoderError::InvalidOutput)
    );
    assert_eq!(
        parse_result(
            Some(7),
            b"error",
            1024,
            Executor::AiDocumentNote,
            "attempt-1:qoder".to_owned(),
        )
        .err(),
        Some(QoderError::NonZeroExit(Some(7)))
    );
    assert_eq!(
        parse_result(
            Some(0),
            &result_output(TRANSLATION, "1"),
            1024,
            Executor::AiDocumentNote,
            "attempt-1:qoder".to_owned(),
        )
        .err(),
        Some(QoderError::InvalidOutput)
    );
}
