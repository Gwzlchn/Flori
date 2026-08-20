use std::{fmt::Write, fs, path::PathBuf};

use flori_core::{
    AiModelCapability, CreateRunnerSlot, ErrorCode, JobId, RegisterRunnerRequest, RunnerTool,
    RunnerToolCapability, Sha256Digest,
};
use flori_store::Store;
use sha2::{Digest, Sha256};
use sqlx::{Row, SqlitePool, sqlite::SqliteConnectOptions};

struct TestDatabase {
    directory: PathBuf,
    path: PathBuf,
}

impl TestDatabase {
    fn new() -> Self {
        let directory =
            std::env::temp_dir().join(format!("flori-runner-control-{}", JobId::generate()));
        fs::create_dir(&directory).expect("create test directory");
        let path = directory.join("flori.db");
        Self { directory, path }
    }

    async fn pool(&self) -> SqlitePool {
        SqlitePool::connect_with(
            SqliteConnectOptions::new()
                .filename(&self.path)
                .foreign_keys(true),
        )
        .await
        .expect("connect test database")
    }
}

impl Drop for TestDatabase {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.directory).expect("remove test directory");
    }
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("write digest");
    }
    Sha256Digest::parse(output).expect("digest")
}

fn capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![
            RunnerToolCapability {
                tool: RunnerTool::PdfExtractor,
                version: "1.2.3".into(),
            },
            RunnerToolCapability {
                tool: RunnerTool::CodexCli,
                version: "0.9".into(),
            },
        ],
        ai_models: vec![AiModelCapability {
            model: "gpt-5.6".into(),
            efforts: vec!["medium".into(), "high".into()],
        }],
    }
}

fn slot(
    name: &str,
    tags: &[&str],
    max_concurrency: u16,
    default: Option<(&str, &str)>,
) -> CreateRunnerSlot {
    CreateRunnerSlot {
        name: name.into(),
        tags: tags.iter().map(|value| (*value).into()).collect(),
        max_concurrency,
        default_model: default.map(|value| value.0.into()),
        default_effort: default.map(|value| value.1.into()),
    }
}

#[tokio::test]
async fn registration_is_single_use_expiring_and_authenticates_only_the_long_digest() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let pool = database.pool().await;
    let expired_digest = digest("expired-registration");
    let expired = store
        .create_runner_slot(
            &slot("expired-runner", &["media"], 1, None),
            &expired_digest,
            10,
            1,
        )
        .await
        .expect("expired slot");
    assert_eq!(
        store
            .register_runner(
                &expired_digest,
                &digest("expired-long"),
                &capabilities(),
                10
            )
            .await
            .expect_err("registration token expires at its boundary")
            .code(),
        ErrorCode::CredentialUnavailable
    );

    let registration_digest = digest("registration-token");
    let long_digest = digest("long-token");
    let runner_id = store
        .create_runner_slot(
            &slot("runner-one", &["media", "ai"], 2, Some(("gpt-5.6", "high"))),
            &registration_digest,
            100,
            1,
        )
        .await
        .expect("runner slot");
    assert_eq!(
        store
            .register_runner(&registration_digest, &long_digest, &capabilities(), 2)
            .await
            .expect("register once"),
        runner_id
    );
    assert_eq!(
        store
            .register_runner(
                &registration_digest,
                &digest("other-long"),
                &capabilities(),
                3
            )
            .await
            .expect_err("registration token cannot replay")
            .code(),
        ErrorCode::CredentialUnavailable
    );
    assert_eq!(
        store
            .authenticate_runner(&digest("wrong-token"), 4)
            .await
            .expect_err("wrong token")
            .code(),
        ErrorCode::CredentialUnavailable
    );
    assert_eq!(
        store
            .authenticate_runner(&long_digest, 4)
            .await
            .expect("long token"),
        runner_id
    );

    let row = sqlx::query(
        "SELECT state,registration_token_digest,registration_expires_at_ms,config_revision, \
             tags_json,tools_json,ai_models_json,last_seen_at_ms FROM runners WHERE id=?",
    )
    .bind(runner_id.to_string())
    .fetch_one(&pool)
    .await
    .expect("registered row");
    assert_eq!(row.try_get::<String, _>("state").expect("state"), "enabled");
    assert_eq!(
        row.try_get::<Option<String>, _>("registration_token_digest")
            .expect("registration digest"),
        None
    );
    assert_eq!(
        row.try_get::<Option<i64>, _>("registration_expires_at_ms")
            .expect("registration expiry"),
        None
    );
    assert_eq!(
        row.try_get::<i64, _>("config_revision").expect("revision"),
        1
    );
    let tags_json: String = row.try_get("tags_json").expect("tags JSON");
    let tools_json: String = row.try_get("tools_json").expect("tools JSON");
    let models_json: String = row.try_get("ai_models_json").expect("models JSON");
    assert_eq!(
        serde_json::from_str::<Vec<String>>(&tags_json).expect("tags"),
        ["ai", "media"]
    );
    serde_json::from_str::<Vec<RunnerToolCapability>>(&tools_json).expect("strict tools");
    let models =
        serde_json::from_str::<Vec<AiModelCapability>>(&models_json).expect("strict models");
    assert_eq!(models[0].efforts, ["high", "medium"]);
    assert_eq!(
        row.try_get::<Option<i64>, _>("last_seen_at_ms")
            .expect("last seen"),
        Some(4)
    );

    let expired_state: String = sqlx::query_scalar("SELECT state FROM runners WHERE id=?")
        .bind(expired.to_string())
        .fetch_one(&pool)
        .await
        .expect("expired slot remains disabled");
    assert_eq!(expired_state, "disabled");
}

#[tokio::test]
async fn registration_rejects_default_capability_and_duplicate_inventory_drift() {
    let database = TestDatabase::new();
    let store = Store::open(&database.path).await.expect("store");
    let registration_digest = digest("capability-registration");
    store
        .create_runner_slot(
            &slot("runner-two", &["ai"], 1, Some(("missing-model", "high"))),
            &registration_digest,
            100,
            1,
        )
        .await
        .expect("slot accepts desired default before probe");
    assert_eq!(
        store
            .register_runner(
                &registration_digest,
                &digest("long-two"),
                &capabilities(),
                2
            )
            .await
            .expect_err("reported inventory lacks configured default")
            .code(),
        ErrorCode::CapabilityMismatch
    );

    let duplicate = RegisterRunnerRequest {
        tools: vec![
            capabilities().tools[0].clone(),
            capabilities().tools[0].clone(),
        ],
        ai_models: Vec::new(),
    };
    assert_eq!(
        store
            .register_runner(&registration_digest, &digest("long-three"), &duplicate, 3)
            .await
            .expect_err("duplicate tool capability")
            .code(),
        ErrorCode::InvalidRequest
    );
}
