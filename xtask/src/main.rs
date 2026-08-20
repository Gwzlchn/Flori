//! 仓库统一开发命令入口。

#![forbid(unsafe_code)]

use std::{env, fs, path::Path, process::Command};

const USAGE: &str = "usage: cargo xtask COMMAND [argument]";
const EXPORT: &str = "run -p flori-server -- export-openapi frontend/.generated/openapi.json";
const CLIPPY: &str = "clippy --workspace --all-targets -- -D warnings";
const FRONTEND_CHECK: &str = "compose -f compose.test.yml run --rm frontend-check";
const FOUNDATION: &str = "compose -f compose.test.yml run --rm integration-foundation";
const GIT_FILES: &str = "ls-files --cached --others --exclude-standard -z --";
const CRATES: &str = "flori-core\0flori-store\0flori-pipeline\0flori-server\0flori-runner\0xtask";
const POLICY_FILES: &str = ".sqlx/query-129b64010ee7cddac8b3c36e19e4a31971abab8fb27ce4f580c592c93ded4f66.json\n.sqlx/query-4f1c1f5e91d92b41bfd1c11a7807c9537aa69b42cea2c5d59487df7d55a96da8.json\n.sqlx/query-52200a51171bab442d1b755c7f1ee306d8f1bf1915b8bed0a02b6b7db7d72638.json\n.sqlx/query-7ba858139d4c260f2daf33a15450464f66e1fe77cf4418acd350ad46a502534d.json\n.sqlx/query-beaee9edbaadcefc2febc643ffffad4aa8d7e7cfd5445018f675c00331f58e49.json\n.sqlx/query-d071e3b0fd0367e9cd965a48ef4460302c4d5edf8d2fc98aa5db62b6d2dc7278.json\n.sqlx/query-e019b4d2e713d2806484a4f5f45a44f1267f4ebe6c73131eb8892f8343354d3a.json\n.sqlx/query-f4648a6d7a1cc285b1d4c5ff83bcef075dee50918656c76b22623029f3600d2d.json\n.sqlx/query-feec2dcc6fd9d9f3aa8f79a59c4ab6c6f0d74fcb1dce380462d54fcc4711e88d.json\nCargo.toml\ncompose.dev.yml\ncompose.prod.yml\ncompose.test.yml\ncrates/flori-core/Cargo.toml\ncrates/flori-core/src/artifact.rs\ncrates/flori-core/src/enums.rs\ncrates/flori-core/src/ids.rs\ncrates/flori-core/src/job.rs\ncrates/flori-core/src/lib.rs\ncrates/flori-core/src/materialize.rs\ncrates/flori-core/src/openapi.rs\ncrates/flori-core/src/runner_claim.rs\ncrates/flori-core/src/runner_protocol.rs\ncrates/flori-pipeline/Cargo.toml\ncrates/flori-runner/Cargo.toml\ncrates/flori-server/Cargo.toml\ncrates/flori-store/Cargo.toml\ncrates/flori-store/migrations/0001_v1.sql\ndocker/runner.Dockerfile\ndocker/server.Dockerfile\nfrontend/Dockerfile\nxtask/Cargo.toml";
const RUST_FORBIDDEN: &str =
    "serde_json::Value\0serde_yaml_ng::Value\0serde(alias\0serde(untagged\0serde(flatten\0Unknown(";
const CORE_TYPES: &str = "PipelineId\0PipelineRevisionId\0SourceId\0SourceInputId\0JobId\0TaskId\0AttemptId\0ArtifactId\0RunnerId\0PromptSnapshotId\0UploadId\0CredentialId\0AiUsageId\0DomainId\0CollectionId\0GlossaryTermId\0ConceptOccurrenceId\0EvidenceId\0SearchChunkId\0QrSessionId\0RequestId\0SourceKind\0JobTrigger\0JobState\0TaskState\0AttemptState\0RunnerState\0CredentialKind\0AiTool\0UsageOrigin\0ArtifactKind\0UploadOwnerKind\0UploadState\0ArtifactOrigin\0ArtifactRetention\0AiUsageState\0JobEventScope\0CollectionKind\0GlossaryTermState\0EvidenceLocatorKind\0Executor\0RunnerTool\0RerunMode\0ArtifactWhen\0TaskLogLevel\0SystemHealthStatus\0JobEventKind\0ErrorCode\0ArtifactDeclaration\0ArtifactManifestSchema\0ArtifactManifest\0ArtifactManifestEntry\0Sha256Digest\0PromptSnapshotPrompt\0PromptSnapshotProfile\0PromptSnapshot\0CompiledTaskSpec\0TaskInputReference\0TaskInputBindings\0JobInputs\0CreateRemoteSource\0CreateJobRequest\0AiRunnerSelection\0RerunJobRequest\0CreatedSource\0CreatedJob\0PendingTaskCommit\0PendingAttemptUpload\0PendingMaterializedArtifact\0PendingMaterializeCommit\0RunnerToolCapability\0AiModelCapability\0RunnerTags\0RunnerTools\0AiModels\0ResolvedArtifact\0ResolvedSourceInput\0ResolvedSource\0ResolvedPrompt\0ResolvedProfile\0ResolvedTaskInputs\0SecretCredential\0SecretInputs\0TaskClaim\0RegisterRunnerRequest\0RegisterRunnerResponse\0CreateRunnerSlot\0CreateRunnerSlotResponse\0RenewLeaseResponse\0LogFrame\0LogCursor\0TaskLogEvent\0UsageUpdate\0UsageAck\0StartUploadRequest\0StartUploadResponse\0UploadCursor\0VerifyUploadRequest\0VerifyUploadResponse\0CompleteAttemptRequest\0FailAttemptRequest\0AttemptAck\0ErrorResponse\0ErrorBody";
const TS_FORBIDDEN: &str =
    "as unknown as\0@ts-ignore\0@ts-nocheck\0as any\0: any\0any[]\0fetch(\0XMLHttpRequest\0axios";

#[derive(Debug, Eq, PartialEq)]
enum Task {
    Check,
    Test(Option<String>),
    Integration,
    Image(String),
    DiffBudget(String),
    Janitor(bool),
}

fn main() {
    if let Err(error) = parse(env::args().skip(1).collect()).and_then(run) {
        eprintln!("xtask: {error}");
        std::process::exit(1);
    }
}

fn parse(args: Vec<String>) -> Result<Task, String> {
    let values: Vec<_> = args.iter().map(String::as_str).collect();
    match values.as_slice() {
        ["check"] => Ok(Task::Check),
        ["test"] => Ok(Task::Test(None)),
        ["test", filter]
            if valid_filter(filter) && (!filter.starts_with("flori-") || is_crate(filter)) =>
        {
            Ok(Task::Test(Some((*filter).into())))
        }
        ["integration", "foundation"] => Ok(Task::Integration),
        ["image", name] if image(name).is_some() => Ok(Task::Image((*name).into())),
        ["diff-budget", base]
            if !base.is_empty() && !base.starts_with(['-', '.', '/']) && !base.contains("..") =>
        {
            Ok(Task::DiffBudget((*base).into()))
        }
        ["janitor", "--dry-run"] => Ok(Task::Janitor(false)),
        ["janitor", "--apply"] => Ok(Task::Janitor(true)),
        _ => Err(USAGE.into()),
    }
}

fn valid_filter(value: &str) -> bool {
    !value.is_empty()
        && !value.starts_with('-')
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_-:".contains(&byte))
}

fn is_crate(value: &str) -> bool {
    CRATES.split('\0').any(|name| name == value)
}

fn image(name: &str) -> Option<(&'static str, Option<&'static str>)> {
    match name {
        "edge" => Some(("frontend/Dockerfile", Some("edge"))),
        "server" => Some(("docker/server.Dockerfile", None)),
        "runner-media" => Some(("docker/runner.Dockerfile", Some("runner-media"))),
        "runner-ai-qoder" => Some(("docker/runner.Dockerfile", Some("runner-ai-qoder"))),
        "runner-ai-codex" => Some(("docker/runner.Dockerfile", Some("runner-ai-codex"))),
        _ => None,
    }
}

fn run(task: Task) -> Result<(), String> {
    let root = Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or("xtask has no repository parent")?
        .canonicalize()
        .map_err(|error| error.to_string())?;
    match task {
        Task::Check => {
            policy_gate(&root)?;
            command(&root, "cargo", "fmt --all -- --check")?;
            command(&root, "cargo", CLIPPY)?;
            command(&root, "docker", FRONTEND_CHECK)
        }
        Task::Test(filter) => match filter.as_deref() {
            Some(name) if is_crate(name) => {
                execute(&root, Command::new("cargo").args(["test", "-p", name]))
            }
            Some(module) => execute(
                &root,
                Command::new("cargo").args(["test", "--workspace", module]),
            ),
            None => command(&root, "cargo", "test --workspace"),
        },
        Task::Integration => {
            command(&root, "cargo", EXPORT)?;
            command(&root, "docker", FOUNDATION)
        }
        Task::Image(name) => {
            if name == "edge" {
                command(&root, "cargo", EXPORT)?;
            }
            let proxy = match env::var("FLORI_BUILD_PROXY") {
                Ok(proxy)
                    if !proxy.is_empty()
                        && matches!(name.as_str(), "runner-ai-qoder" | "runner-ai-codex") =>
                {
                    Some(proxy)
                }
                Ok(_) | Err(env::VarError::NotPresent) => None,
                Err(error) => return Err(format!("invalid FLORI_BUILD_PROXY: {error}")),
            };
            execute(
                &root,
                &mut image_command(
                    &name,
                    proxy.as_deref(),
                    env::var_os("GITHUB_ACTIONS").is_some(),
                )?,
            )
        }
        Task::DiffBudget(base) => diff_budget(&root, &base),
        Task::Janitor(apply) => janitor(&root, apply),
    }
}

fn command(root: &Path, program: &str, args: &str) -> Result<(), String> {
    execute(
        root,
        Command::new(program).args(args.split_ascii_whitespace()),
    )
}

fn execute(root: &Path, command: &mut Command) -> Result<(), String> {
    let status = command
        .current_dir(root)
        .status()
        .map_err(|error| format!("cannot start command: {error}"))?;
    status
        .success()
        .then_some(())
        .ok_or_else(|| format!("command exited with {status}"))
}

fn image_command(name: &str, proxy: Option<&str>, gha_cache: bool) -> Result<Command, String> {
    let (dockerfile, target) = image(name).ok_or("unknown image target")?;
    let mut command = Command::new("docker");
    command.args(["build", "--file", dockerfile]);
    if let Some(target) = target {
        command.args(["--target", target]);
    }
    if let Some(proxy) = proxy {
        command.args(["--build-arg", &format!("HTTP_PROXY={proxy}")]);
        command.args(["--build-arg", &format!("HTTPS_PROXY={proxy}")]);
    }
    if gha_cache {
        let scope = match name {
            "edge" | "server" => name,
            _ => "runner",
        };
        command.args(["--cache-from", &format!("type=gha,scope=flori-{scope}")]);
        command.args([
            "--cache-to",
            &format!("type=gha,mode=max,scope=flori-{scope}"),
        ]);
    }
    let context = if name == "edge" { "frontend" } else { "." };
    command.args(["--tag", &format!("flori-{name}:local"), context]);
    Ok(command)
}

fn policy_gate(root: &Path) -> Result<(), String> {
    command(root, "cargo", EXPORT)?;
    command(root, "sha256sum", "--check --quiet xtask/policy.sha256")?;
    let mut files = repository_files(
        root,
        &[
            ".sqlx",
            "*Cargo.toml",
            "compose*.yml",
            "*Dockerfile",
            "crates/flori-core",
            "crates/flori-store/migrations",
            "frontend/.generated",
        ],
    )?;
    files.sort();
    if files.join("\n") != POLICY_FILES {
        return Err(format!("architecture inventory changed: {files:?}"));
    }
    let rules = [("crates", RUST_FORBIDDEN), ("frontend/src", TS_FORBIDDEN)];
    for (directory, patterns) in rules {
        for relative in repository_files(root, &[directory])? {
            let path = root.join(&relative);
            let text = fs::read_to_string(&path).map_err(|error| error.to_string())?;
            if let Some(pattern) = patterns.split('\0').find(|pattern| text.contains(pattern)) {
                return Err(format!(
                    "forbidden pattern {pattern:?} in {}",
                    path.display()
                ));
            }
            if relative.starts_with("crates/")
                && !relative.starts_with("crates/flori-core/")
                && CORE_TYPES.split('\0').any(|name| {
                    ["enum", "struct", "type"]
                        .iter()
                        .any(|kind| text.contains(&format!("{kind} {name}")))
                })
            {
                return Err(format!("core type redeclared in {relative}"));
            }
            if relative != "frontend/src/api/client.ts" && text.contains(".generated") {
                return Err(format!("generated API import outside client: {relative}"));
            }
        }
    }
    Ok(())
}

fn diff_budget(root: &Path, base: &str) -> Result<(), String> {
    policy_gate(root)?;
    let output = Command::new("git")
        .args(["diff", "--numstat", "--no-renames", base, "--"])
        .current_dir(root)
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().into());
    }
    let (mut added, deleted, mut files) = parse_numstat(&String::from_utf8_lossy(&output.stdout));
    let (untracked_lines, untracked_files) = untracked_numstat(root)?;
    added = added.saturating_add(untracked_lines);
    files = files.saturating_add(untracked_files);
    let lines = added.saturating_sub(deleted);
    let max_lines = env_limit("FLORI_DIFF_MAX_LINES", 300)?;
    let max_files = env_limit("FLORI_DIFF_MAX_FILES", 10)?;
    eprintln!("diff budget: {lines}/{max_lines} net lines, {files}/{max_files} files");
    (lines <= max_lines && files <= max_files)
        .then_some(())
        .ok_or_else(|| "handwritten diff exceeds the approved budget".into())
}

fn untracked_numstat(root: &Path) -> Result<(usize, usize), String> {
    let output = Command::new("git")
        .args(["ls-files", "--others", "--exclude-standard", "-z", "--"])
        .current_dir(root)
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().into());
    }
    output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|path| !path.is_empty())
        .try_fold((0usize, 0usize), |(lines, files), raw| {
            let relative = std::str::from_utf8(raw).map_err(|_| "non-UTF-8 path")?;
            if excluded(relative) {
                return Ok((lines, files));
            }
            let bytes = fs::read(root.join(relative)).map_err(|error| error.to_string())?;
            if bytes.contains(&0) {
                return Ok((lines, files));
            }
            let count = bytes.iter().filter(|byte| **byte == b'\n').count()
                + usize::from(!bytes.is_empty() && !bytes.ends_with(b"\n"));
            Ok((lines.saturating_add(count), files + 1))
        })
}

fn repository_files(root: &Path, paths: &[&str]) -> Result<Vec<String>, String> {
    let mut command = Command::new("git");
    let output = command
        .args(GIT_FILES.split_ascii_whitespace())
        .args(paths)
        .current_dir(root)
        .output()
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().into());
    }
    output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|raw| !raw.is_empty())
        .map(|raw| {
            std::str::from_utf8(raw)
                .map(str::to_owned)
                .map_err(|_| "non-UTF-8 path".into())
        })
        .collect()
}

fn env_limit(name: &str, default: usize) -> Result<usize, String> {
    match env::var(name) {
        Ok(value) => value
            .parse()
            .map_err(|_| format!("{name} must be a non-negative integer")),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(error) => Err(format!("invalid {name}: {error}")),
    }
}

fn parse_numstat(input: &str) -> (usize, usize, usize) {
    input
        .lines()
        .filter_map(|line| {
            let mut field = line.splitn(3, '\t');
            let (added, deleted, path) = (field.next()?, field.next()?, field.next()?);
            if added == "-" || excluded(path) {
                None
            } else {
                Some((added.parse().ok()?, deleted.parse().ok()?))
            }
        })
        .fold((0usize, 0usize, 0usize), |(a, d, f), (add, del)| {
            (a.saturating_add(add), d.saturating_add(del), f + 1)
        })
}

fn excluded(path: &str) -> bool {
    matches!(
        path.rsplit('/').next(),
        Some("Cargo.lock" | "package-lock.json" | "pnpm-lock.yaml" | "yarn.lock")
    ) || path.starts_with("target/")
        || path.starts_with(".sqlx/")
        || path.starts_with("frontend/.generated/")
        || path.starts_with("frontend/dist/")
}

fn janitor(root: &Path, apply: bool) -> Result<(), String> {
    for relative in ["target", "frontend/dist", "frontend/.generated"] {
        let candidate = root.join(relative);
        if !candidate.exists() {
            continue;
        }
        let metadata = fs::symlink_metadata(&candidate).map_err(|error| error.to_string())?;
        if metadata.file_type().is_symlink() {
            return Err(format!("refusing symlink: {}", candidate.display()));
        }
        let path = candidate
            .canonicalize()
            .map_err(|error| error.to_string())?;
        if path == root || !path.starts_with(root) {
            return Err(format!(
                "refusing path outside repository: {}",
                path.display()
            ));
        }
        if apply {
            fs::remove_dir_all(&path).map_err(|error| error.to_string())?;
        } else {
            println!("would remove {}", path.display());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
