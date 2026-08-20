use std::{ffi::OsStr, path::Path};

use flori_core::{
    AiResultEnvelope, AiResultSchema, Executor, TermsManifest, TermsManifestSchema, UsageOrigin,
    UsageUpdate,
};
use flori_runner::{
    CodexAdapterError, CodexWebSearchObservation, build_codex_command, parse_codex_output,
};

fn translation() -> AiResultEnvelope {
    AiResultEnvelope::DocumentTranslate {
        schema: AiResultSchema::V1,
        translation_markdown: "# 译文".into(),
    }
}

fn document_note() -> AiResultEnvelope {
    AiResultEnvelope::DocumentNote {
        schema: AiResultSchema::V1,
        smart_note_markdown: "# 笔记".into(),
        summary_markdown: "摘要".into(),
        terms: TermsManifest {
            schema: TermsManifestSchema::V1,
            terms: Vec::new(),
        },
    }
}

fn result_json(result: &AiResultEnvelope) -> String {
    serde_json::to_string(result).expect("serialize result")
}

fn agent_event(result: &str) -> String {
    format!(
        r#"{{"type":"item.completed","item":{{"id":"item_1","type":"agent_message","text":{}}}}}"#,
        serde_json::to_string(result).expect("encode agent text")
    )
}

fn completed_usage(fields: &str) -> String {
    format!(r#"{{"type":"turn.completed","usage":{{{fields}}}}}"#)
}

fn stream(result: &str, usage: &str) -> String {
    [
        r#"{"type":"thread.started","thread_id":"thread-1"}"#.to_owned(),
        r#"{"type":"turn.started"}"#.to_owned(),
        r#"{"type":"item.started","item":{"id":"item_0","type":"reasoning","text":""}}"#.to_owned(),
        agent_event(result),
        completed_usage(usage),
    ]
    .join("\n")
}

fn args(command: &flori_runner::CodexCommand) -> Vec<&str> {
    command
        .args
        .iter()
        .map(|value| value.to_str().expect("UTF-8 test argument"))
        .collect()
}

#[test]
fn command_is_ephemeral_read_only_and_keeps_prompt_out_of_argv() {
    let prompt = "private-prompt-value";
    let command = build_codex_command(
        Executor::AiDocumentNote,
        "gpt-5.3-codex",
        "high",
        Path::new("/attempt"),
        Path::new("/attempt/schema.json"),
        Path::new("/attempt/result.json"),
        prompt,
    )
    .expect("command");
    let args = args(&command);

    assert_eq!(command.program, "codex");
    assert_eq!(command.stdin, prompt);
    assert!(
        args.windows(2)
            .any(|pair| pair == ["--ask-for-approval", "never"])
    );
    assert!(
        args.windows(2)
            .any(|pair| pair == ["--sandbox", "read-only"])
    );
    assert!(args.contains(&"--ephemeral"));
    assert!(args.contains(&"--strict-config"));
    assert!(args.contains(&"--ignore-user-config"));
    assert!(args.contains(&"--search"));
    assert!(
        args.windows(2)
            .any(|pair| pair == ["--model", "gpt-5.3-codex"])
    );
    assert!(args.contains(&"model_reasoning_effort=\"high\""));
    assert!(args.contains(&"history.persistence=\"none\""));
    assert_eq!(args.last(), Some(&"-"));
    assert!(!command.args.iter().any(|arg| arg == OsStr::new(prompt)));
}

#[test]
fn websearch_policy_is_fixed_by_executor() {
    let translate = build_codex_command(
        Executor::AiDocumentTranslate,
        "model-1",
        "medium",
        Path::new("/attempt"),
        Path::new("/schema"),
        Path::new("/result"),
        "translate",
    )
    .expect("translate command");
    let translate_args = args(&translate);
    assert!(!translate_args.contains(&"--search"));
    assert!(translate_args.contains(&"web_search=\"disabled\""));

    let video = build_codex_command(
        Executor::AiVideoNote,
        "model-1",
        "medium",
        Path::new("/attempt"),
        Path::new("/schema"),
        Path::new("/result"),
        "note",
    )
    .expect("video command");
    assert!(args(&video).contains(&"--search"));

    assert_eq!(
        build_codex_command(
            Executor::DocumentExtract,
            "model-1",
            "medium",
            Path::new("/attempt"),
            Path::new("/schema"),
            Path::new("/result"),
            "extract",
        ),
        Err(CodexAdapterError::UnsupportedExecutor)
    );
}

#[test]
fn parses_strict_result_and_preserves_observed_total_tokens() {
    let result = result_json(&translation());
    let output = parse_codex_output(
        Executor::AiDocumentTranslate,
        "invoke-1",
        Some(0),
        &stream(
            &result,
            r#""input_tokens":120,"cached_input_tokens":100,"cache_write_input_tokens":0,"output_tokens":30,"reasoning_output_tokens":10"#,
        ),
        &result,
    )
    .expect("valid output");

    assert_eq!(output.result, translation());
    assert_eq!(
        output.usage,
        UsageUpdate::Final {
            invocation_key: "invoke-1".into(),
            origin: UsageOrigin::Observed,
            input_tokens: Some(120),
            output_tokens: Some(30),
            cost_micros: None,
            credits_micros: None,
        }
    );
}

#[test]
fn rejects_missing_or_invented_token_fields() {
    let result = result_json(&translation());
    let missing = stream(
        &result,
        r#""cached_input_tokens":10,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0"#,
    );
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(0),
            &missing,
            &result,
        ),
        Err(CodexAdapterError::InvalidEvent { line: 5 })
    );

    let invented_total = stream(
        &result,
        r#""input_tokens":20,"cached_input_tokens":10,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0,"total_tokens":25"#,
    );
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(0),
            &invented_total,
            &result,
        ),
        Err(CodexAdapterError::InvalidEvent { line: 5 })
    );
}

#[test]
fn rejects_duplicate_unknown_and_mismatched_results() {
    let result = result_json(&translation());
    let usage = r#""input_tokens":20,"cached_input_tokens":10,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0"#;
    let duplicate = [
        r#"{"type":"thread.started","thread_id":"thread-1"}"#.to_owned(),
        r#"{"type":"turn.started"}"#.to_owned(),
        agent_event(&result),
        agent_event(&result),
        completed_usage(usage),
    ]
    .join("\n");
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(0),
            &duplicate,
            &result,
        ),
        Err(CodexAdapterError::InvalidOutput("duplicate agent message"))
    );

    let unknown = r#"{"type":"turn.recovered"}"#;
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(0),
            unknown,
            &result,
        ),
        Err(CodexAdapterError::InvalidEvent { line: 1 })
    );

    let other = result_json(&AiResultEnvelope::DocumentTranslate {
        schema: AiResultSchema::V1,
        translation_markdown: "different".into(),
    });
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(0),
            &stream(&result, usage),
            &other,
        ),
        Err(CodexAdapterError::InvalidOutput("result mismatch"))
    );
}

#[test]
fn records_only_observed_websearch_urls_and_rejects_extra_item_fields() {
    let result = result_json(&document_note());
    let usage = r#""input_tokens":20,"cached_input_tokens":10,"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0"#;
    let events = [
        r#"{"type":"thread.started","thread_id":"thread-1"}"#.to_owned(),
        r#"{"type":"turn.started"}"#.to_owned(),
        r#"{"type":"item.completed","item":{"id":"search-1","type":"web_search","query":"find docs","action":{"type":"search","query":"find docs","queries":null}}}"#.to_owned(),
        r#"{"type":"item.completed","item":{"id":"search-2","type":"web_search","query":"open docs","action":{"type":"open_page","url":"https://example.com/docs"}}}"#.to_owned(),
        agent_event(&result),
        completed_usage(usage),
    ]
    .join("\n");
    let output = parse_codex_output(
        Executor::AiDocumentNote,
        "invoke-1",
        Some(0),
        &events,
        &result,
    )
    .expect("websearch output");
    assert_eq!(
        output.websearch,
        vec![
            CodexWebSearchObservation {
                query: "find docs".into(),
                url: None,
            },
            CodexWebSearchObservation {
                query: "open docs".into(),
                url: Some("https://example.com/docs".into()),
            },
        ]
    );

    let duplicate_id = events.replacen(r#""id":"search-1""#, r#""id":"item-1","id":"search-1""#, 1);
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentNote,
            "invoke-1",
            Some(0),
            &duplicate_id,
            &result,
        ),
        Err(CodexAdapterError::InvalidEvent { line: 3 })
    );

    let extra = events.replacen(
        r#""type":"web_search","query""#,
        r#""type":"web_search","unexpected":true,"query""#,
        1,
    );
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentNote,
            "invoke-1",
            Some(0),
            &extra,
            &result,
        ),
        Err(CodexAdapterError::InvalidEvent { line: 3 })
    );
}

#[test]
fn nonzero_exit_wins_without_parsing_untrusted_output() {
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            Some(9),
            "not json",
            "not json",
        ),
        Err(CodexAdapterError::ProcessFailed(Some(9)))
    );
    assert_eq!(
        parse_codex_output(
            Executor::AiDocumentTranslate,
            "invoke-1",
            None,
            "partial output",
            "missing result",
        ),
        Err(CodexAdapterError::ProcessFailed(None))
    );
}
