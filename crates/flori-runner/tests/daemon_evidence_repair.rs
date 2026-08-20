#[path = "support/evidence_repair.rs"]
mod evidence_repair;
#[path = "support/daemon.rs"]
mod support;

use std::{fs, net::TcpListener, time::Duration};

use flori_core::{
    AiAudit, ArtifactId, ArtifactKind, ArtifactWhen, AttemptId, AttemptState, ErrorCode,
    EvidenceId, Executor, ResolvedTaskInputs, UsageUpdate,
};
use flori_runner::{DaemonConfig, RunnerClient, run_ai_daemon};
use tokio::sync::watch;

use evidence_repair::*;
use support::*;

#[derive(Clone, Copy)]
enum Case {
    RepairSucceeds,
    RepairFails,
    PrimaryValid,
    InvalidOuter,
}

#[tokio::test]
async fn invalid_evidence_repairs_once_and_succeeds() {
    run_case(Case::RepairSucceeds).await;
}

#[tokio::test]
async fn invalid_repair_fails_after_exactly_two_calls() {
    run_case(Case::RepairFails).await;
}

#[tokio::test]
async fn valid_primary_result_never_calls_repair() {
    run_case(Case::PrimaryValid).await;
}

#[tokio::test]
async fn invalid_outer_result_never_calls_repair() {
    run_case(Case::InvalidOuter).await;
}

async fn run_case(case: Case) {
    let root = temp_root(match case {
        Case::RepairSucceeds => "repair-success",
        Case::RepairFails => "repair-failure",
        Case::PrimaryValid => "primary-valid",
        Case::InvalidOuter => "invalid-outer",
    });
    let source_id = ArtifactId::generate();
    let structure_id = ArtifactId::generate();
    let document = document(source_id);
    let evidence_id = EvidenceId::generate();
    let valid = note(source_id, evidence_id, true);
    let invalid = note(source_id, evidence_id, false);
    let (primary, repair) = match case {
        Case::RepairSucceeds => (invalid.clone(), valid.clone()),
        Case::RepairFails => (invalid.clone(), invalid),
        Case::PrimaryValid => (valid.clone(), valid),
        Case::InvalidOuter => (valid.clone(), valid),
    };
    let primary_outer = if matches!(case, Case::InvalidOuter) {
        "{}".to_owned()
    } else {
        qoder(&primary)
    };
    let repair_outer = qoder(&repair);
    let counter = root.join("calls");
    let prompts = root.join("prompts");
    let executable = script(
        &root,
        "fake-qoder",
        &format!(
            "n=0; [ ! -f '{counter}' ] || n=$(cat '{counter}'); n=$((n+1)); printf '%s' \"$n\" > '{counter}'; cat >> '{prompts}'; printf '\\n---\\n' >> '{prompts}'; if [ \"$n\" -eq 1 ]; then printf '%s' '{primary_outer}'; else printf '%s' '{repair_outer}'; fi",
            counter = counter.display(),
            prompts = prompts.display(),
        ),
    );
    let bytes = serde_json::to_vec(&document).expect("document JSON");
    let exec_id = AttemptId::generate();
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base_url = format!("http://{}", listener.local_addr().expect("address"));
    let claim = claim(
        exec_id,
        Executor::AiDocumentNote,
        ResolvedTaskInputs::AiDocumentNote {
            document: artifact_with_id(&base_url, &bytes, structure_id),
            prompt: prompt("write a cited note"),
            profile: None,
        },
        vec![
            declaration(
                "smart_note",
                ArtifactKind::SmartNote,
                ArtifactWhen::OnSuccess,
            ),
            declaration("summary", ArtifactKind::Summary, ArtifactWhen::OnSuccess),
            declaration("terms", ArtifactKind::Terms, ArtifactWhen::OnSuccess),
            declaration("audit", ArtifactKind::AiAudit, ArtifactWhen::Always),
        ],
        5_000,
    );
    let (stop, mut cancel) = watch::channel(false);
    let server = server(listener, claim, bytes.clone(), digest(&bytes), stop);
    let client = RunnerClient::new(&base_url, "token").expect("client");
    run_ai_daemon(
        &client,
        &config(&root, executable, Duration::from_secs(1)),
        &mut cancel,
    )
    .await
    .expect("daemon");
    let observed = server.join().expect("server");

    let calls = if matches!(case, Case::PrimaryValid | Case::InvalidOuter) {
        1
    } else {
        2
    };
    assert_eq!(
        fs::read_to_string(counter).expect("calls"),
        calls.to_string()
    );
    assert_eq!(observed.usage.len(), calls * 2);
    for (index, key) in ["primary", "repair"].into_iter().take(calls).enumerate() {
        assert!(
            matches!(&observed.usage[index * 2], UsageUpdate::Started { invocation_key, .. } if invocation_key == key)
        );
        assert!(
            matches!(&observed.usage[index * 2 + 1], UsageUpdate::Final { invocation_key, .. } if invocation_key == key)
        );
    }
    let audit: AiAudit = serde_json::from_slice(observed.uploaded.get("audit").expect("audit"))
        .expect("strict audit");
    assert_eq!(
        audit
            .usage_invocation_keys
            .iter()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["primary", "repair"][..calls]
    );
    assert_eq!(audit.exit_code, Some(0));
    assert!(!audit.timed_out);
    let expected_output = if calls == 1 {
        &primary_outer
    } else {
        &repair_outer
    };
    assert_eq!(audit.output_sha256, digest(expected_output.as_bytes()));
    let prompts = fs::read_to_string(prompts).expect("prompts");
    assert_eq!(
        prompts.matches("EVIDENCE PRECHECK ERROR").count(),
        calls - 1
    );
    match case {
        Case::RepairSucceeds | Case::PrimaryValid => {
            assert_eq!(observed.state, AttemptState::Succeeded);
            assert_eq!(observed.error, None);
            assert!(observed.uploaded.contains_key("smart_note"));
        }
        Case::RepairFails => {
            assert_eq!(observed.state, AttemptState::Failed);
            assert_eq!(observed.error, Some(ErrorCode::EvidenceInvalid));
            assert_eq!(observed.uploaded.len(), 1);
        }
        Case::InvalidOuter => {
            assert_eq!(observed.state, AttemptState::Failed);
            assert_eq!(observed.error, Some(ErrorCode::ExecutorFailed));
            assert_eq!(observed.uploaded.len(), 1);
        }
    }
    fs::remove_dir_all(root).expect("cleanup");
}
