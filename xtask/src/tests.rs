use super::*;

fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(ToString::to_string).collect()
}

#[test]
fn parses_only_declared_commands() {
    assert_eq!(parse(strings(&["check"])), Ok(Task::Check));
    assert_eq!(parse(strings(&["test"])), Ok(Task::Test(None)));
    assert_eq!(
        parse(strings(&["test", "flori-core"])),
        Ok(Task::Test(Some("flori-core".into())))
    );
    assert_eq!(
        parse(strings(&["integration", "foundation"])),
        Ok(Task::Integration)
    );
    assert_eq!(
        parse(strings(&["janitor", "--apply"])),
        Ok(Task::Janitor(true))
    );
    assert!(parse(strings(&["check", "extra"])).is_err());
    assert!(parse(strings(&["test", "--workspace"])).is_err());
    assert!(parse(strings(&["test", "flori-future"])).is_err());
    assert!(parse(strings(&["integration", "other"])).is_err());
    assert!(parse(strings(&["diff-budget", "../main"])).is_err());
    assert!(parse(strings(&["diff-budget", "origin/rust-vnext"])).is_ok());
}

#[test]
fn exposes_only_fixed_image_mappings() {
    assert_eq!(image("edge"), Some(("frontend/Dockerfile", Some("edge"))));
    assert_eq!(image("server"), Some(("docker/server.Dockerfile", None)));
    for name in ["runner-media", "runner-ai-qoder", "runner-ai-codex"] {
        assert_eq!(image(name).unwrap().0, "docker/runner.Dockerfile");
    }
    assert_eq!(image("custom"), None);

    let edge = image_command("edge", None, false).unwrap();
    let edge_args: Vec<_> = edge.get_args().map(|arg| arg.to_string_lossy()).collect();
    assert_eq!(edge_args.last().unwrap(), "frontend");

    let ai = image_command("runner-ai-codex", Some("http://proxy:8080"), true).unwrap();
    let ai_args: Vec<_> = ai.get_args().map(|arg| arg.to_string_lossy()).collect();
    assert!(
        ai_args
            .iter()
            .any(|arg| arg == "HTTP_PROXY=http://proxy:8080")
    );
    assert!(
        ai_args
            .iter()
            .any(|arg| arg == "HTTPS_PROXY=http://proxy:8080")
    );
    assert_eq!(ai_args.last().unwrap(), ".");
    assert!(
        ai_args
            .iter()
            .any(|arg| arg == "type=gha,scope=flori-runner")
    );
    assert!(
        ai_args
            .iter()
            .any(|arg| arg == "type=gha,mode=max,scope=flori-runner")
    );

    let server = image_command("server", None, true).unwrap();
    assert!(
        server
            .get_args()
            .any(|arg| arg == "type=gha,scope=flori-server")
    );
}

#[test]
fn numstat_excludes_generated_lock_and_binary_files() {
    let input = concat!(
        "20\t3\tcrates/flori-core/src/lib.rs\n",
        "50\t0\tCargo.lock\n",
        "75\t0\tfrontend/package-lock.json\n",
        "90\t0\tfrontend/.generated/api.ts\n",
        "40\t0\t.sqlx/query.json\n",
        "-\t-\ttests/fixture.pdf\n",
        "1\t8\tfrontend/src/App.vue\n",
    );
    assert_eq!(parse_numstat(input), (21, 11, 2));
}

#[test]
fn numstat_counts_untracked_handwritten_files() {
    let base = env::temp_dir().join(format!("flori-xtask-untracked-{}", std::process::id()));
    fs::create_dir_all(base.join("src")).unwrap();
    assert!(
        Command::new("git")
            .args(["init", "--quiet"])
            .current_dir(&base)
            .status()
            .unwrap()
            .success()
    );
    fs::write(base.join("src/new.rs"), "one\ntwo").unwrap();
    fs::write(base.join("Cargo.lock"), "ignored\n").unwrap();
    fs::write(base.join("fixture.bin"), [0, 1, 2]).unwrap();
    assert_eq!(untracked_numstat(&base).unwrap(), (2, 1));
    fs::remove_dir_all(&base).unwrap();
}

#[test]
fn janitor_rejects_symlink_outside_repository() {
    let base = env::temp_dir().join(format!("flori-xtask-{}", std::process::id()));
    let root = base.join("repo");
    let outside = base.join("outside");
    fs::create_dir_all(&root).unwrap();
    fs::create_dir_all(&outside).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(&outside, root.join("target")).unwrap();
    #[cfg(unix)]
    assert!(janitor(&root.canonicalize().unwrap(), false).is_err());
    fs::remove_dir_all(&base).unwrap();
}

#[test]
fn janitor_dry_run_preserves_allowed_directory() {
    let base = env::temp_dir().join(format!("flori-xtask-dry-{}", std::process::id()));
    let root = base.join("repo");
    fs::create_dir_all(root.join("frontend/.generated")).unwrap();
    janitor(&root.canonicalize().unwrap(), false).unwrap();
    assert!(root.join("frontend/.generated").exists());
    fs::remove_dir_all(&base).unwrap();
}
