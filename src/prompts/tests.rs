use std::ffi::OsString;

use super::*;

fn env_with(dir: &Path) -> Vec<(OsString, OsString)> {
    vec![("FLORI_PROMPTS_DIR".into(), dir.into())]
}

#[test]
fn built_in_texts_compose_to_what_steps_sent_before() {
    for name in PromptName::ALL {
        assert_eq!(compose(&[builtin(name)]), builtin(name), "{name}");
        let prompt = Prompt {
            text: builtin(name).into(),
            domain: None,
        };
        assert_eq!(prompt.instructions(), builtin(name), "{name}");
    }
    // The note prompt and the image instructions were joined by one blank line.
    let video = builtin(PromptName::NoteVideo);
    let images = builtin(PromptName::NoteImages);
    assert_eq!(compose(&[video, images]), format!("{video}\n{images}"));
    assert_eq!(
        compose(&["a\n\n\n", "", "\n\nb  \n", "  c"]),
        "a\n\nb\n\n  c\n"
    );
}

#[test]
fn tidy_keeps_the_text_and_drops_what_surrounds_it() {
    assert_eq!(tidy("\r\n\n  - 一\r\n  - 二 \r\n\n\t "), "  - 一\n  - 二");
    assert_eq!(tidy(" \n "), "");
    assert_eq!(
        tidy(builtin(PromptName::Translate)) + "\n",
        builtin(PromptName::Translate)
    );
}

#[test]
fn without_the_directory_or_its_file_a_step_sends_the_built_in_text() {
    let builtin_only = |name| Prompt {
        text: builtin(name).to_owned(),
        domain: None,
    };
    let empty = tempfile::tempdir().unwrap();
    for env in [Vec::new(), env_with(empty.path())] {
        for name in PromptName::ALL {
            assert_eq!(Prompt::load(&env, name).unwrap(), builtin_only(name));
        }
    }
}

#[test]
fn the_override_replaces_the_built_in_text_and_the_requirement_goes_last() {
    let dir = tempfile::tempdir().unwrap();
    let prompts = dir.path().join("prompts");
    let env = env_with(&prompts);
    let mut stored = Stored::default();
    stored
        .overrides
        .insert(PromptName::Translate, "译成中文。".into());
    stored
        .domain
        .insert(PromptName::Translate, "\r\n- 术语保留英文。\r\n".into());
    stored
        .domain
        .insert(PromptName::NoteVideo, "- 写出指令集。".into());
    write_dir(&prompts, "计算机系统结构", &stored).unwrap();

    let translate = Prompt::load(&env, PromptName::Translate).unwrap();
    assert_eq!(translate.text, "译成中文。");
    let section = "## 领域要求(计算机系统结构)\n\n- 术语保留英文。\n";
    assert_eq!(translate.domain.as_deref(), Some(section));
    assert_eq!(translate.instructions(), format!("译成中文。\n\n{section}"));
    let video = Prompt::load(&env, PromptName::NoteVideo).unwrap();
    assert_eq!(video.text, builtin(PromptName::NoteVideo));
    assert_eq!(
        video.domain.as_deref(),
        Some("## 领域要求(计算机系统结构)\n\n- 写出指令集。\n")
    );
    for name in [
        PromptName::NoteArxiv,
        PromptName::NoteImages,
        PromptName::Punctuate,
    ] {
        let prompt = Prompt::load(&env, name).unwrap();
        assert_eq!((prompt.text.as_str(), prompt.domain), (builtin(name), None));
    }

    // The next attempt sees what is stored then: nothing left from the one before.
    write_dir(&prompts, "计算机系统结构", &Stored::default()).unwrap();
    for name in PromptName::ALL {
        let prompt = Prompt::load(&env, name).unwrap();
        assert_eq!((prompt.text.as_str(), prompt.domain), (builtin(name), None));
    }
    let mut files: Vec<String> = fs::read_dir(&prompts)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    files.sort();
    assert_eq!(
        files,
        [
            "note-arxiv.md",
            "note-images.md",
            "note-video.md",
            "punctuate.md",
            "translate.md"
        ]
    );
}

#[test]
fn an_empty_requirement_file_is_no_requirement() {
    let dir = tempfile::tempdir().unwrap();
    fs::write(dir.path().join("punctuate.domain.md"), " \n").unwrap();
    let prompt = Prompt::load(&env_with(dir.path()), PromptName::Punctuate).unwrap();
    assert_eq!(prompt.domain, None);
}
