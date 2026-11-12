//! The five prompts AI steps send. The texts in prompts/ are the defaults; the web keeps an
//! override per prompt and a requirement per domain and prompt in SQLite. Steps never read the
//! database: before each attempt of note or translate the executor writes the job's prompts to
//! `jobs/<id>/prompts/` and passes FLORI_PROMPTS_DIR, and a step without it, as in tests and
//! `just step`, sends the built-in texts. Rules: contract 1 "提示词" in docs/contracts.md.

use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
};

use crate::{ai::Env, pipeline::PromptName};

/// Longest override and longest domain requirement, in characters. A prompt is a page of
/// instructions, a requirement a few points added to one.
pub const TEXT_CHARS: usize = 20_000;
pub const DOMAIN_CHARS: usize = 4_000;

pub fn builtin(name: PromptName) -> &'static str {
    match name {
        PromptName::NoteVideo => include_str!("../prompts/note-video.md"),
        PromptName::NoteArxiv => include_str!("../prompts/note-arxiv.md"),
        PromptName::NoteImages => include_str!("../prompts/note-images.md"),
        PromptName::Translate => include_str!("../prompts/translate.md"),
        PromptName::Punctuate => include_str!("../prompts/punctuate.md"),
    }
}

/// What pages call the prompt.
pub fn label(name: PromptName) -> &'static str {
    match name {
        PromptName::NoteVideo => "视频笔记",
        PromptName::NoteArxiv => "论文笔记",
        PromptName::NoteImages => "笔记配图",
        PromptName::Translate => "翻译",
        PromptName::Punctuate => "加标点",
    }
}

/// Text as it is stored and compared: line breaks as `\n`, without blank lines before or
/// whitespace after it.
pub fn tidy(text: &str) -> String {
    trim_blank_edges(&text.replace("\r\n", "\n")).to_owned()
}

/// `text` without its leading blank lines, spaces-only ones included, and its trailing
/// whitespace. The first line keeps its indent.
fn trim_blank_edges(text: &str) -> &str {
    let text = text.trim_end();
    let first = text.len() - text.trim_start().len();
    let line = text[..first].rfind('\n').map_or(0, |newline| newline + 1);
    &text[line..]
}

/// Stored texts by prompt name: the overrides of built-in prompts and one domain's requirements.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Stored {
    pub overrides: BTreeMap<PromptName, String>,
    pub domain: BTreeMap<PromptName, String>,
}

impl Stored {
    /// What a step that starts now sends for `name`, before any domain requirement.
    pub fn text(&self, name: PromptName) -> &str {
        self.overrides
            .get(&name)
            .map_or(builtin(name), String::as_str)
    }
}

fn file(name: PromptName) -> String {
    format!("{name}.md")
}

fn domain_file(name: PromptName) -> String {
    format!("{name}.domain.md")
}

/// Writes every prompt the job's AI steps may send into `dir`, replacing what an earlier attempt
/// left: `<name>.md` with the override or the built-in text, and `<name>.domain.md` with the
/// domain's requirement under its heading where there is one.
pub fn write_dir(dir: &Path, domain: &str, stored: &Stored) -> io::Result<()> {
    match fs::remove_dir_all(dir) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => {}
        result => result?,
    }
    fs::create_dir_all(dir)?;
    for name in PromptName::ALL {
        fs::write(dir.join(file(name)), stored.text(name))?;
        if let Some(text) = stored.domain.get(&name) {
            let section = format!("## 领域要求({domain})\n\n{}\n", tidy(text));
            fs::write(dir.join(domain_file(name)), section)?;
        }
    }
    Ok(())
}

/// One prompt as a step sends it.
#[derive(Debug, Clone, PartialEq)]
pub struct Prompt {
    pub text: String,
    /// The job's domain requirement under its heading. It goes after all of the prompt's
    /// instructions: for note, after note-images when images go along.
    pub domain: Option<String>,
}

impl Prompt {
    /// `name` from FLORI_PROMPTS_DIR. Without the variable or the file the built-in text, and
    /// no domain requirement.
    pub fn load(env: &Env, name: PromptName) -> io::Result<Prompt> {
        let dir = env
            .iter()
            .find(|(k, _)| k == "FLORI_PROMPTS_DIR")
            .map(|(_, v)| PathBuf::from(v));
        let read = |file: String| match &dir {
            None => Ok(None),
            Some(dir) => match fs::read_to_string(dir.join(file)) {
                Ok(text) => Ok(Some(text)),
                Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(None),
                Err(err) => Err(err),
            },
        };
        Ok(Prompt {
            text: read(file(name))?.unwrap_or_else(|| builtin(name).to_owned()),
            domain: read(domain_file(name))?.filter(|text| !text.trim().is_empty()),
        })
    }

    /// The instructions and then the domain requirement.
    pub fn instructions(&self) -> String {
        compose(&[&self.text, self.domain.as_deref().unwrap_or_default()])
    }
}

/// Instruction blocks joined by one blank line and ending in one newline, empty ones left out.
/// The built-in texts compose to what the steps sent before prompts could be edited, so cached
/// translations stay valid.
pub fn compose(blocks: &[&str]) -> String {
    let mut out = String::new();
    for block in blocks {
        let block = trim_blank_edges(block);
        if block.is_empty() {
            continue;
        }
        if !out.is_empty() {
            out.push_str("\n\n");
        }
        out.push_str(block);
    }
    out.push('\n');
    out
}

#[cfg(test)]
mod tests;
