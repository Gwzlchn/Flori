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

#[test]
fn submitted_links_parse_to_canonical_sources() {
    let bv = "https://www.bilibili.com/video/BV1GJ411x7h7";
    let yt = "https://www.youtube.com/watch?v=dQw4w9WgXcQ";
    let cases = [
        (
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            Platform::Bilibili,
            "BV1GJ411x7h7",
            bv,
        ),
        (
            "https://m.bilibili.com/video/BV1GJ411x7h7?p=3",
            Platform::Bilibili,
            "BV1GJ411x7h7",
            bv,
        ),
        (
            "http://www.bilibili.com/video/BV1GJ411x7h7/?spm_id_from=333.788#reply",
            Platform::Bilibili,
            "BV1GJ411x7h7",
            bv,
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
            Platform::Youtube,
            "dQw4w9WgXcQ",
            yt,
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?si=abc",
            Platform::Youtube,
            "dQw4w9WgXcQ",
            yt,
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            Platform::Youtube,
            "dQw4w9WgXcQ",
            yt,
        ),
        (
            "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
            Platform::Youtube,
            "dQw4w9WgXcQ",
            yt,
        ),
        (
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            Platform::Youtube,
            "dQw4w9WgXcQ",
            yt,
        ),
        (
            " https://arxiv.org/abs/2401.12345v2 ",
            Platform::Arxiv,
            "2401.12345",
            "https://arxiv.org/abs/2401.12345",
        ),
        (
            "https://arxiv.org/pdf/2401.12345v1.pdf",
            Platform::Arxiv,
            "2401.12345",
            "https://arxiv.org/abs/2401.12345",
        ),
        (
            "https://arxiv.org/html/2401.12345v3/#S2",
            Platform::Arxiv,
            "2401.12345",
            "https://arxiv.org/abs/2401.12345",
        ),
        (
            "https://arxiv.org/abs/1501.0001",
            Platform::Arxiv,
            "1501.0001",
            "https://arxiv.org/abs/1501.0001",
        ),
        (
            "2401.12345",
            Platform::Arxiv,
            "2401.12345",
            "https://arxiv.org/abs/2401.12345",
        ),
        (
            "arXiv:2401.12345v2",
            Platform::Arxiv,
            "2401.12345",
            "https://arxiv.org/abs/2401.12345",
        ),
        (
            "hep-th/9711200v1",
            Platform::Arxiv,
            "hep-th/9711200",
            "https://arxiv.org/abs/hep-th/9711200",
        ),
        (
            "https://arxiv.org/abs/math.GT/0309136",
            Platform::Arxiv,
            "math.GT/0309136",
            "https://arxiv.org/abs/math.GT/0309136",
        ),
    ];
    for (input, platform, id, url) in cases {
        let source = parse_source(input).unwrap_or_else(|| panic!("{input}"));
        assert_eq!(
            (source.platform, source.id.as_str(), source.url.as_str()),
            (platform, id, url),
            "{input}"
        );
        assert_eq!(
            parse_source(&source.url),
            Some(source.clone()),
            "canonical {input}"
        );
    }
    for input in [
        "https://b23.tv/abc123",
        "https://www.bilibili.com/video/av170001",
        "https://www.bilibili.com/video/BV1GJ411x7h",
        "https://example.com/video/BV1GJ411x7h7",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/@3blue1brown",
        "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://arxiv.org/abs/2401.123",
        "https://arxiv.org/list/cs.AI/recent",
        "HEP-TH/9711200",
        "",
    ] {
        assert_eq!(parse_source(input), None, "{input}");
    }
}

#[test]
fn feed_pick_ids_are_checked() {
    assert!(video_source(Platform::Bilibili, "BV1GJ411x7h7").is_some());
    assert!(video_source(Platform::Bilibili, "BV1GJ411x7h/").is_none());
    assert!(video_source(Platform::Youtube, "dQw4w9WgXc_").is_some());
    assert!(video_source(Platform::Youtube, "dQw4w9WgXc").is_none());
    assert!(video_source(Platform::Arxiv, "2401.12345").is_none());
}

#[test]
fn feed_urls_parse_to_kind_and_normal_form() {
    let season = "https://space.bilibili.com/100/lists/200?type=season";
    let series = "https://space.bilibili.com/100/lists/200?type=series";
    let channel = "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/videos";
    let cases = [
        (
            "https://space.bilibili.com/946974",
            FeedKind::BiliUp,
            "https://space.bilibili.com/946974",
        ),
        (
            "space.bilibili.com/946974/upload/video?tid=0",
            FeedKind::BiliUp,
            "https://space.bilibili.com/946974",
        ),
        (
            "https://space.bilibili.com/100/channel/collectiondetail?sid=200",
            FeedKind::BiliSeason,
            season,
        ),
        (season, FeedKind::BiliSeason, season),
        (
            "https://space.bilibili.com/100/channel/seriesdetail?sid=200&ctype=0",
            FeedKind::BiliSeries,
            series,
        ),
        (
            "https://space.bilibili.com/100/lists/200?type=series&spm=x",
            FeedKind::BiliSeries,
            series,
        ),
        (
            "https://space.bilibili.com/247209804/favlist?ftype=create&fid=987654",
            FeedKind::BiliFav,
            "https://space.bilibili.com/247209804/favlist?fid=987654",
        ),
        (
            "https://www.youtube.com/@3blue1brown",
            FeedKind::YtChannel,
            "https://www.youtube.com/@3blue1brown/videos",
        ),
        (
            "youtube.com/@3blue1brown/streams",
            FeedKind::YtChannel,
            "https://www.youtube.com/@3blue1brown/videos",
        ),
        (
            "https://m.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/featured",
            FeedKind::YtChannel,
            channel,
        ),
        (
            "https://www.youtube.com/user/minutephysics",
            FeedKind::YtChannel,
            "https://www.youtube.com/user/minutephysics/videos",
        ),
        (
            "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab&si=x",
            FeedKind::YtPlaylist,
            "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab",
        ),
    ];
    for (input, kind, url) in cases {
        let parsed = parse_feed(input).unwrap_or_else(|| panic!("{input}"));
        assert_eq!(parsed, (kind, url.to_owned()), "{input}");
        assert_eq!(
            parse_feed(url),
            Some(parsed),
            "normal form of {input} parses back"
        );
    }
    for input in [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab",
        "https://www.bilibili.com/video/BV1GJ411x7h7",
        "https://example.com/@3blue1brown",
        "https://space.bilibili.com.evil.example/100",
        "https://space.bilibili.com/100/favlist",
        "https://space.bilibili.com/100/lists/200",
        "https://space.bilibili.com/100/channel/seriesdetail",
        "https://space.bilibili.com/abc",
        "https://www.youtube.com/@",
        "",
    ] {
        assert_eq!(parse_feed(input), None, "{input}");
    }
    assert_eq!(feed_kind_arg(FeedKind::BiliSeason), "bili_season");
    let json = serde_json::to_string(&FeedKind::YtPlaylist).unwrap();
    assert_eq!(json, format!("\"{}\"", feed_kind_arg(FeedKind::YtPlaylist)));
}
