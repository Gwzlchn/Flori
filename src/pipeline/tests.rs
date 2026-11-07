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
