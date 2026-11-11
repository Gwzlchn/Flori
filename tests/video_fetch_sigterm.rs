//! `flori step video-fetch` under a real SIGTERM, sent to its process group the way the executor
//! cancels or times out a step: the step exits 10, the SESSDATA cookie file is gone, nothing in
//! the job directory holds SESSDATA and no tool outlives the step. yt-dlp and curl are the fakes
//! in tests/fixtures/video/fake; the video id tells the fake yt-dlp where to stall.

use std::{
    fs,
    io::Read,
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, ExitStatus, Stdio},
    thread,
    time::{Duration, Instant},
};

use rustix::process::{Pid, Signal, kill_process_group};

const SESSDATA: &str = "fake-sessdata-for-sigterm";

fn wait_until(what: &str, mut ready: impl FnMut() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(10);
    while !ready() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        thread::sleep(Duration::from_millis(20));
    }
}

fn names(dir: &Path) -> Vec<String> {
    let mut names: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    names
}

fn all_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            files.extend(all_files(&path));
        } else {
            files.push(path);
        }
    }
    files
}

/// Live processes in group `pgid` as `pid (comm)`, read from /proc without signalling anyone.
/// Zombies are left out: an orphan waits for whoever reaps it, which is not the step. The
/// group id stays reserved while any member exists, so it cannot name another group.
fn live_members(pgid: i32) -> Vec<String> {
    let mut members = Vec::new();
    for entry in fs::read_dir("/proc").unwrap().flatten() {
        let Ok(stat) = fs::read_to_string(entry.path().join("stat")) else {
            continue;
        };
        // `pid (comm) state ppid pgrp ...`; comm may contain spaces.
        let Some((head, rest)) = stat.rsplit_once(')') else {
            continue;
        };
        let fields: Vec<&str> = rest.split_whitespace().collect();
        if fields.len() > 2 && fields[2] == pgid.to_string() && fields[0] != "Z" {
            members.push(format!("{head})"));
        }
    }
    members
}

/// The step in its own process group, which is the only one this test signals. If an assertion
/// fails while the step is still unreaped, its pid and so the group id are still reserved, and
/// dropping this SIGKILLs that group.
struct Step {
    child: Child,
    pgid: Pid,
}

impl Drop for Step {
    fn drop(&mut self) {
        if let Ok(None) = self.child.try_wait() {
            let _ = kill_process_group(self.pgid, Signal::KILL);
            let _ = self.child.wait();
        }
    }
}

/// Starts video-fetch for Bilibili `id` with a SESSDATA, waits for the cookie file and for the
/// fake yt-dlp to have started `runs` times, then sends SIGTERM to the step's process group.
/// Returns the step's stderr.
fn terminate_during(id: &str, runs: usize) -> String {
    let job = tempfile::tempdir().unwrap();
    let tmp = tempfile::tempdir().unwrap();
    let home = tempfile::tempdir().unwrap();
    let job_json = serde_json::json!({
        "job_id": 7,
        "pipeline": "video",
        "source": {"platform": "bilibili", "id": id, "url": "https://example.invalid/"},
        "domain": "ml",
        "collection": null,
        "translate": "auto",
        "ai": {"tool": "claude", "model_note": "m", "model_translate": "m", "cli_version": "1"},
        "created": "2026-11-08T10:00:00+08:00",
        "public_url": "http://nas.lan:8780",
        "vault_name": "flori",
    });
    fs::write(job.path().join("job.json"), job_json.to_string()).unwrap();
    let out = job.path().join("steps/01-video-fetch.tmp");
    fs::create_dir_all(&out).unwrap();
    let cache = job.path().join("cache/video-fetch");
    fs::create_dir_all(&cache).unwrap();
    let fakes = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/video/fake");

    let child = Command::new(env!("CARGO_BIN_EXE_flori"))
        .args(["step", "video-fetch"])
        .arg(job.path())
        .arg(&out)
        .env_clear()
        .env("FLORI_CACHE_DIR", &cache)
        .env("PATH", format!("{}:/usr/bin:/bin", fakes.display()))
        .env("TMPDIR", tmp.path())
        .env("HOME", home.path())
        .env("FLORI_BILI_SESSDATA", SESSDATA)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .process_group(0)
        .spawn()
        .unwrap();
    let pgid = Pid::from_child(&child);
    let raw_pgid = pgid.as_raw_nonzero().get();
    let mut step = Step { child, pgid };

    let cookie = tmp.path().join("flori-bili-7.cookies");
    wait_until("the cookie file", || cookie.exists());
    let log = job.path().join("steps/fake-yt-dlp.log");
    wait_until("the fake yt-dlp", || {
        fs::read_to_string(&log).is_ok_and(|text| text.lines().count() == runs)
    });
    let members = live_members(raw_pgid);
    assert!(members.len() >= 2, "{id}: step and yt-dlp: {members:?}");
    kill_process_group(pgid, Signal::TERM).unwrap();

    let mut status: Option<ExitStatus> = None;
    wait_until("the step to exit", || {
        status = step.child.try_wait().unwrap();
        status.is_some()
    });
    wait_until("the step's tools to exit", || {
        live_members(raw_pgid).is_empty()
    });
    let mut stderr = String::new();
    step.child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();

    assert_eq!(status.unwrap().code(), Some(10), "{id}: {stderr}");
    assert_eq!(names(tmp.path()), Vec::<String>::new(), "{id}");
    assert!(!out.join("manifest.json").exists(), "{id}");
    for file in all_files(job.path()) {
        let text = String::from_utf8_lossy(&fs::read(&file).unwrap()).into_owned();
        assert!(
            !text.contains(SESSDATA) && !text.contains("SESSDATA"),
            "{id}: {}",
            file.display()
        );
    }
    stderr
}

#[test]
fn sigterm_while_downloading_the_video() {
    terminate_during("BVhang", 1);
}

/// The subtitle run that SIGTERM kills must not pass for "no subtitles" and exit 0.
#[test]
fn sigterm_while_downloading_subtitles() {
    terminate_during("BVsubs-stall", 2);
}

/// A tool that ignores SIGTERM leaves the step's own handler as the only way out, and
/// `kill_on_drop` has to take the tool down with it.
#[test]
fn sigterm_while_a_tool_ignores_it() {
    let stderr = terminate_during("BVignore-term", 1);
    assert!(stderr.contains("stopped by SIGTERM"), "{stderr}");
}
