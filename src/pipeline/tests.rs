use std::fs;

use super::*;

#[test]
fn plan_drops_translate_only_when_off() {
    let names = |p, t| {
        plan(p, t)
            .into_iter()
            .map(|s| s.name.as_str())
            .collect::<Vec<_>>()
    };
    assert_eq!(
        names(Pipeline::Video, TranslateMode::Auto),
        [
            "video-fetch",
            "video-transcribe",
            "video-frames",
            "note",
            "translate",
            "publish"
        ]
    );
    assert_eq!(
        names(Pipeline::Arxiv, TranslateMode::Off),
        ["arxiv-fetch", "arxiv-convert", "note", "publish"]
    );
}

#[test]
fn ai_steps_run_once_and_arxiv_runs_in_python() {
    for name in StepName::ALL {
        let spec = name.spec();
        if spec.resource == Resource::Ai {
            assert_eq!(spec.attempts, 1, "{name}");
        }
        let python = matches!(spec.program, StepProgram::Python { .. });
        assert_eq!(
            python,
            matches!(name, StepName::ArxivFetch | StepName::ArxivConvert)
        );
    }
}

#[test]
fn step_names_round_trip() {
    for name in StepName::ALL {
        assert_eq!(name.as_str().parse::<StepName>(), Ok(name));
        assert_eq!(serde_json::to_string(&name).unwrap(), format!("\"{name}\""));
    }
    assert!("Video-Fetch".parse::<StepName>().is_err());
}

#[test]
fn find_step_ignores_tmp_and_other_names() {
    let job = tempfile::tempdir().unwrap();
    let steps = job.path().join("steps");
    assert_eq!(find_step(job.path(), StepName::Note).unwrap(), None);
    fs::create_dir_all(steps.join("04-note.tmp")).unwrap();
    fs::create_dir_all(steps.join("05-translate")).unwrap();
    assert_eq!(find_step(job.path(), StepName::Note).unwrap(), None);
    fs::create_dir_all(steps.join("04-note")).unwrap();
    assert_eq!(
        find_step(job.path(), StepName::Note).unwrap(),
        Some(steps.join("04-note"))
    );
    assert_eq!(step_dir_name(4, StepName::Note), "04-note");
}

#[test]
fn manifest_round_trips_and_rejects_unknown_fields() {
    let dir = tempfile::tempdir().unwrap();
    let mut manifest = Manifest {
        title: Some("t".into()),
        ..Manifest::default()
    };
    manifest.files.insert("note".into(), "note.md".into());
    manifest.assets.push("frame-1000.jpg".into());
    manifest
        .meta
        .insert("duration_ms".into(), MetaValue::Int(1000));
    manifest
        .meta
        .insert("origin".into(), MetaValue::Text("subtitle".into()));
    manifest
        .meta
        .insert("tags".into(), MetaValue::List(vec!["a".into()]));
    write_manifest(dir.path(), &manifest).unwrap();
    assert_eq!(read_manifest(dir.path()).unwrap(), manifest);

    fs::write(
        dir.path().join(MANIFEST_FILE),
        r#"{"title":null,"files":{},"assets":[],"meta":{},"extra":1}"#,
    )
    .unwrap();
    assert!(read_manifest(dir.path()).is_err());
}

#[test]
fn escape_markdown_neutralizes_obsidian_syntax() {
    assert_eq!(
        escape_markdown("call `__init__` #rust"),
        "call \\`\\_\\_init\\_\\_\\` \\#rust"
    );
    assert_eq!(
        escape_markdown("$5-$10 50%% <b>"),
        "\\$5-\\$10 50\\%\\% \\<b\\>"
    );
    assert_eq!(escape_markdown("~~~ 音乐 ~~~"), "\\~~~ 音乐 ~~~");
    assert_eq!(
        escape_markdown("- a\n+ b\n[[x]]"),
        "\\- a\n\\+ b\n\\[\\[x\\]\\]"
    );
    assert_eq!(escape_markdown("中文 a-b"), "中文 a-b");
}

#[test]
fn clock_switches_to_hours_at_one_hour() {
    assert_eq!(clock(0), "00:00");
    assert_eq!(clock(61_999), "01:01");
    assert_eq!(clock(3_599_000), "59:59");
    assert_eq!(clock(3_600_000), "1:00:00");
}

#[test]
fn sections_round_trip_through_the_frames_step() {
    let job = tempfile::tempdir().unwrap();
    let frames = job
        .path()
        .join("steps")
        .join(step_dir_name(3, StepName::VideoFrames));
    fs::create_dir_all(&frames).unwrap();
    let sections = vec![
        Section {
            start_ms: 0,
            frames: vec!["frame-0.jpg".into()],
            text: "hello".into(),
        },
        Section {
            start_ms: 30_000,
            frames: vec![],
            text: "  ".into(),
        },
    ];
    write_json(&frames.join("sections.json"), &sections).unwrap();
    let mut manifest = Manifest::default();
    manifest
        .files
        .insert("sections".into(), "sections.json".into());
    write_manifest(&frames, &manifest).unwrap();

    assert_eq!(read_sections(job.path()).unwrap(), sections);
    assert!(sections[0].has_text() && !sections[1].has_text());
    let missing = step_file(job.path(), StepName::VideoFrames, "transcript").unwrap_err();
    assert_eq!(missing.exit, StepExit::Permanent);
    let absent = prior_step(job.path(), StepName::Note).unwrap_err();
    assert_eq!(absent.exit, StepExit::Permanent);
}
