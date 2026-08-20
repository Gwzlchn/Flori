//! 仓库统一开发命令入口。

#![forbid(unsafe_code)]

use std::{env, fs, path::Path, process::Command};

mod policy;

const EXPORT: &str = "run -p flori-server -- export-openapi frontend/.generated/openapi.json";
const CLIPPY: &str = "clippy --workspace --all-targets -- -D warnings";
const FRONTEND_CHECK: &str = "compose -f compose.test.yml run --rm frontend-check";
const FOUNDATION: &str = "compose -f compose.test.yml run --rm integration-foundation";
const GIT_FILES: &str = "ls-files --cached --others --exclude-standard -z --";
const CRATES: &str = "flori-core\0flori-store\0flori-pipeline\0flori-server\0flori-runner\0xtask";

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
        _ => Err("usage: cargo xtask COMMAND [argument]".into()),
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
        command.args(["--network", "host"]);
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
    policy::check(root)
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
