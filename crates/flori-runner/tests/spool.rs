use std::fs;
use std::path::PathBuf;

use flori_core::{
    AttemptId, CredentialKind, ErrorCode, LogFrame, SecretCredential, SecretInputs, Sha256Digest,
    UploadId,
};
use flori_runner::{Spool, SpoolUpload};

fn temporary_root(label: &str) -> PathBuf {
    std::env::temp_dir().join(format!("flori-runner-{label}-{}", AttemptId::generate()))
}

fn digest(byte: char) -> Sha256Digest {
    Sha256Digest::parse(byte.to_string().repeat(64)).expect("digest")
}

#[test]
fn spool_redacts_before_persisting_and_resumes_after_ack() {
    let root = temporary_root("redaction");
    let spool = Spool::open(&root, 16 * 1024).expect("open spool");
    let exec_id = AttemptId::generate();
    let credential_value = r#"cookie\"value"#;
    let secrets = SecretInputs {
        credential: Some(SecretCredential {
            kind: CredentialKind::BilibiliCookie,
            value: credential_value.to_owned(),
        }),
    };
    let frame = LogFrame {
        sequence: 1,
        sha256: digest('a'),
        line: serde_json::to_string(&format!("cookie={credential_value}")).expect("log JSON"),
    };
    let redacted = spool
        .queue_log(exec_id, &frame, &secrets)
        .expect("queue redacted log");
    assert!(!redacted.line.contains(credential_value));
    assert!(redacted.line.contains("[REDACTED]"));
    let bytes = fs::read(root.join(exec_id.to_string()).join("logs.json")).expect("spool bytes");
    let persisted = String::from_utf8(bytes).expect("UTF-8 spool");
    let escaped = serde_json::to_string(credential_value).expect("escaped credential");
    assert!(!persisted.contains(credential_value));
    assert!(!persisted.contains(&escaped[1..escaped.len() - 1]));
    assert!(persisted.contains("[REDACTED]"));

    spool.acknowledge_logs(exec_id, 1).expect("ack log");
    let next = LogFrame {
        sequence: 2,
        sha256: digest('b'),
        line: "next".to_owned(),
    };
    let queued = spool
        .queue_log(exec_id, &next, &SecretInputs::default())
        .expect("resume at server cursor");
    assert_eq!(spool.logs(exec_id).expect("logs"), vec![queued]);
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
        line: "x".repeat(1024),
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
