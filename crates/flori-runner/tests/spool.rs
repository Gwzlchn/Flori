use std::fs;
use std::path::PathBuf;

use flori_core::{
    AttemptId, CredentialKind, ErrorCode, LogFrame, SecretCredential, SecretInputs, Sha256Digest,
    TaskLogLevel, TaskLogLine, UploadId,
};
use flori_runner::{Spool, SpoolUpload};
use sha2::{Digest, Sha256};

fn temporary_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!("flori-runner-{label}-{}", AttemptId::generate()))
}

fn digest(byte: char) -> Sha256Digest {
    Sha256Digest::parse(byte.to_string().repeat(64)).expect("digest")
}

fn sha256(bytes: &[u8]) -> Sha256Digest {
    Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("SHA-256")
}

fn log_line(message: impl Into<String>) -> TaskLogLine {
    TaskLogLine {
        timestamp_ms: 7,
        level: TaskLogLevel::Info,
        message: message.into(),
    }
}

#[test]
fn spool_redacts_decoded_secrets_before_persisting() {
    let root = temporary_root("redaction");
    let spool = Spool::open(&root, 16 * 1024).expect("open spool");
    let quoted = r#"quote"\path"#;
    let cases = [
        (
            "TOKEN",
            serde_json::to_string(&log_line("before TOKEN after")).expect("plain line"),
            "TOKEN",
        ),
        (
            "TOKEN",
            r#"{"timestamp_ms":7,"level":"info","message":"before \u0054OKEN after"}"#.to_owned(),
            r#"\u0054OKEN"#,
        ),
        (
            quoted,
            serde_json::to_string(&log_line(format!("before {quoted} after")))
                .expect("escaped line"),
            quoted,
        ),
    ];
    for (index, (secret, line, encoded)) in cases.into_iter().enumerate() {
        let exec_id = AttemptId::generate();
        let secrets = SecretInputs {
            credential: Some(SecretCredential {
                kind: CredentialKind::BilibiliCookie,
                value: secret.to_owned(),
            }),
        };
        let frame = LogFrame {
            sequence: index as u64 + 1,
            sha256: digest('a'),
            line,
        };
        let redacted = spool
            .queue_log(exec_id, &frame, &secrets)
            .expect("queue redacted log");
        assert_eq!(redacted.sequence, frame.sequence);
        assert_eq!(redacted.sha256, sha256(redacted.line.as_bytes()));
        let decoded: TaskLogLine =
            serde_json::from_str(&redacted.line).expect("strict returned line");
        assert_eq!(decoded.message, "before [REDACTED] after");
        assert!(!redacted.line.contains(secret));
        assert!(!redacted.line.contains(encoded));
        let bytes =
            fs::read(root.join(exec_id.to_string()).join("logs.json")).expect("spool bytes");
        let persisted = String::from_utf8(bytes).expect("UTF-8 spool");
        assert_eq!(
            persisted,
            serde_json::to_string(&vec![redacted.clone()]).expect("canonical spool")
        );
        assert!(!persisted.contains(secret));
        assert!(!persisted.contains(encoded));
        let stored = spool.logs(exec_id).expect("stored frames");
        serde_json::from_str::<TaskLogLine>(&stored[0].line).expect("strict stored line");
    }
    fs::remove_dir_all(&root).expect("remove test spool");
}

#[test]
fn spool_canonicalizes_task_logs_and_rejects_invalid_lines() {
    let root = temporary_root("canonical");
    let spool = Spool::open(&root, 16 * 1024).expect("open spool");
    let exec_id = AttemptId::generate();
    let frame = LogFrame {
        sequence: 4,
        sha256: digest('b'),
        line: r#"{ "message":"\u006fk", "level":"info", "timestamp_ms":7 }"#.to_owned(),
    };
    let queued = spool
        .queue_log(exec_id, &frame, &SecretInputs::default())
        .expect("canonical log");
    assert_eq!(queued.sequence, 4);
    assert_eq!(
        queued.line,
        serde_json::to_string(&log_line("ok")).expect("canonical line")
    );
    assert_eq!(queued.sha256, sha256(queued.line.as_bytes()));
    assert_eq!(spool.logs(exec_id).expect("canonical spool"), vec![queued]);
    spool.acknowledge_logs(exec_id, 4).expect("ack log");
    assert!(spool.logs(exec_id).expect("empty spool").is_empty());
    let invalid = LogFrame {
        sequence: 5,
        sha256: digest('c'),
        line: r#"{"timestamp_ms":7,"level":"info","message":"bad","extra":true}"#.to_owned(),
    };
    let error = spool
        .queue_log(AttemptId::generate(), &invalid, &SecretInputs::default())
        .expect_err("strict TaskLogLine");
    assert_eq!(error.code(), ErrorCode::InvalidRequest);
    fs::remove_dir_all(&root).expect("remove test spool");
}

#[test]
fn spool_bounds_bytes_and_keeps_upload_identity_immutable() {
    let root = temporary_root("bounds");
    let spool = Spool::open(&root, 512).expect("open spool");
    let exec_id = AttemptId::generate();
    let upload = SpoolUpload {
        exec_id,
        upload_id: UploadId::generate(),
        name: "note".to_owned(),
        relative_path: "output/note.md".to_owned(),
        size_bytes: 10,
        sha256: digest('a'),
        received_bytes: 3,
    };
    spool.save_upload(&upload).expect("save upload");
    let mut advanced = upload.clone();
    advanced.received_bytes = 8;
    spool.save_upload(&advanced).expect("advance upload");
    assert_eq!(spool.uploads(exec_id).expect("uploads"), vec![advanced]);

    let mut changed = upload.clone();
    changed.sha256 = digest('b');
    let error = spool.save_upload(&changed).expect_err("immutable upload");
    assert_eq!(error.code(), ErrorCode::Conflict);

    let large = LogFrame {
        sequence: 1,
        sha256: digest('c'),
        line: serde_json::to_string(&log_line("x".repeat(1024))).expect("large task log"),
    };
    let error = spool
        .queue_log(exec_id, &large, &SecretInputs::default())
        .expect_err("bounded spool");
    assert_eq!(error.code(), ErrorCode::ArtifactTooLarge);
    spool
        .clear_attempt(exec_id)
        .expect("clear terminal attempt");
    assert!(!root.join(exec_id.to_string()).exists());
    fs::remove_dir_all(&root).expect("remove test spool");
}
