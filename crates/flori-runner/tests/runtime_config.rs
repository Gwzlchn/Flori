#[path = "../src/runtime_config.rs"]
mod runtime_config;

use std::{collections::HashMap, ffi::OsString, path::PathBuf};

use flori_core::AiTool;
use runtime_config::{RuntimeConfigError, parse};

fn args(tool: &str) -> Vec<OsString> {
    ["run", tool].into_iter().map(Into::into).collect()
}

fn environment(tool: AiTool) -> HashMap<&'static str, OsString> {
    let mut values = HashMap::from([
        ("FLORI_SERVER_URL", "https://flori.example.test".into()),
        ("FLORI_RUNNER_TOKEN", "runner-token".into()),
        ("FLORI_RUNNER_MODEL", "gpt-5.3-codex".into()),
        ("FLORI_RUNNER_EFFORT", "high".into()),
        (
            "FLORI_RUNNER_SPOOL_DIR",
            "/var/lib/flori-runner/spool".into(),
        ),
        ("HOME", "/home/flori".into()),
    ]);
    match tool {
        AiTool::QoderCli => values.insert("QODER_CONFIG_DIR", "/home/flori/.qoder".into()),
        AiTool::CodexCli => values.insert("CODEX_HOME", "/home/flori/.codex".into()),
    };
    values
}

fn parse_with(
    tool: &str,
    values: &HashMap<&'static str, OsString>,
) -> Result<runtime_config::RuntimeConfig, RuntimeConfigError> {
    parse(&args(tool), |name| values.get(name).cloned())
}

#[test]
fn accepts_only_two_explicit_run_commands() {
    for invalid in [
        vec![],
        vec!["run"],
        vec!["qoder"],
        vec!["run", "claude"],
        vec!["run", "qoder", "extra"],
    ] {
        let invalid: Vec<OsString> = invalid.into_iter().map(Into::into).collect();
        assert_eq!(
            parse(&invalid, |_| None).err(),
            Some(RuntimeConfigError::InvalidArguments)
        );
    }
}

#[test]
fn parses_qoder_and_codex_without_external_actions() {
    for (name, tool, config_dir) in [
        ("qoder", AiTool::QoderCli, "/home/flori/.qoder"),
        ("codex", AiTool::CodexCli, "/home/flori/.codex"),
    ] {
        let config = parse_with(name, &environment(tool)).expect("valid runtime config");
        assert_eq!(config.tool, tool);
        assert_eq!(config.server_url, "https://flori.example.test");
        assert_eq!(config.token, "runner-token");
        assert_eq!(config.model, "gpt-5.3-codex");
        assert_eq!(config.effort, "high");
        assert_eq!(
            config.spool_dir,
            PathBuf::from("/var/lib/flori-runner/spool")
        );
        assert_eq!(config.home_dir, PathBuf::from("/home/flori"));
        assert_eq!(config.tool_config_dir, PathBuf::from(config_dir));
    }
}

#[test]
fn rejects_every_missing_or_empty_setting_before_a_client_exists() {
    for tool in [AiTool::QoderCli, AiTool::CodexCli] {
        let tool_name = match tool {
            AiTool::QoderCli => "qoder",
            AiTool::CodexCli => "codex",
        };
        let config_name = match tool {
            AiTool::QoderCli => "QODER_CONFIG_DIR",
            AiTool::CodexCli => "CODEX_HOME",
        };
        for name in [
            "FLORI_SERVER_URL",
            "FLORI_RUNNER_TOKEN",
            "FLORI_RUNNER_MODEL",
            "FLORI_RUNNER_EFFORT",
            "FLORI_RUNNER_SPOOL_DIR",
            "HOME",
            config_name,
        ] {
            let mut missing = environment(tool);
            missing.remove(name);
            assert_eq!(
                parse_with(tool_name, &missing).err(),
                Some(RuntimeConfigError::MissingEnvironment(name))
            );

            let mut empty = environment(tool);
            empty.insert(name, OsString::new());
            assert_eq!(
                parse_with(tool_name, &empty).err(),
                Some(RuntimeConfigError::InvalidEnvironment(name))
            );
        }
    }
}

#[test]
fn rejects_remote_http_invalid_urls_and_invalid_identifiers() {
    for invalid_url in [
        "http://flori.example.test",
        "https://user@flori.example.test",
        "https://flori.example.test?token=leak",
        "not-a-url",
    ] {
        let mut values = environment(AiTool::CodexCli);
        values.insert("FLORI_SERVER_URL", invalid_url.into());
        assert_eq!(
            parse_with("codex", &values).err(),
            Some(RuntimeConfigError::InvalidEnvironment("FLORI_SERVER_URL"))
        );
    }
    for (name, value) in [
        ("FLORI_RUNNER_MODEL", "model with spaces"),
        ("FLORI_RUNNER_EFFORT", ""),
        ("FLORI_RUNNER_EFFORT", "effort/value"),
    ] {
        let mut values = environment(AiTool::CodexCli);
        values.insert(name, value.into());
        assert_eq!(
            parse_with("codex", &values).err(),
            Some(RuntimeConfigError::InvalidEnvironment(name))
        );
    }
}

#[test]
fn allows_loopback_http_but_requires_absolute_directories() {
    let mut values = environment(AiTool::QoderCli);
    values.insert("FLORI_SERVER_URL", "http://localhost:8080".into());
    assert!(parse_with("qoder", &values).is_ok());

    for invalid_path in ["relative/path", "/home/flori/../escape", "/"] {
        for name in ["FLORI_RUNNER_SPOOL_DIR", "HOME", "QODER_CONFIG_DIR"] {
            let mut invalid = values.clone();
            invalid.insert(name, invalid_path.into());
            assert_eq!(
                parse_with("qoder", &invalid).err(),
                Some(RuntimeConfigError::InvalidEnvironment(name))
            );
        }
    }
}
