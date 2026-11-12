//! Vault path, title cleaning, frontmatter, assembly in publish.tmp, and idempotent rename.
//! Layout and fields: contract 2 in docs/contracts.md.

use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
};

use serde_json::{Value, json};
use time::{OffsetDateTime, format_description::well_known::Rfc3339, macros::offset};
use unicode_normalization::UnicodeNormalization;

use crate::pipeline::{self, JobSpec, Manifest, MetaValue, StepFailure, StepName, StepResult};

/// Logical names copied into the vault as `<name>.md`, with the label of the link the note gets
/// to each. The note becomes `<title>.md`.
const COPIED: [(&str, &str); 3] = [
    ("transcript", "转写"),
    ("original", "原文"),
    ("translation", "译文"),
];
/// Heading of the note section that becomes `description`, as prompts/note-*.md ask for it.
const SUMMARY_HEADING: &str = "一句话总结";
const TITLE_BYTES: usize = 100;

pub async fn run(job_dir: &Path, out_dir: &Path) -> StepResult {
    let vault = std::env::var_os("FLORI_VAULT_DIR")
        .ok_or_else(|| StepFailure::permanent("FLORI_VAULT_DIR is not set"))?;
    publish(job_dir, out_dir, Path::new(&vault))
}

pub(crate) fn publish(job_dir: &Path, out_dir: &Path, vault_dir: &Path) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let mut title = None;
    let mut files: BTreeMap<String, PathBuf> = BTreeMap::new();
    let mut assets: BTreeMap<String, PathBuf> = BTreeMap::new();
    // Defaults hold when translate is not scheduled.
    let mut language = "unknown".to_owned();
    let mut translated = false;
    let mut source = Vec::new();
    // The title as the platform gave it, before video-fetch made Chinese simplified.
    let mut original_title = None;
    for spec in pipeline::plan(job.pipeline, job.translate) {
        let step = spec.name;
        if step == StepName::Publish {
            continue;
        }
        let (dir, manifest) = pipeline::prior_step(job_dir, step)?;
        title = title.or(manifest.title);
        for (name, rel) in manifest.files {
            let overrides = step == StepName::Translate && name == "transcript";
            if files.insert(name.clone(), dir.join(rel)).is_some() && !overrides {
                return Err(StepFailure::permanent(format!(
                    "{step} repeats file {name}"
                )));
            }
        }
        for asset in manifest.assets {
            if asset.contains('/') || asset.starts_with('.') {
                return Err(StepFailure::permanent(format!("bad asset name {asset:?}")));
            }
            let src = dir.join("assets").join(&asset);
            if assets.insert(asset.clone(), src).is_some() {
                return Err(StepFailure::permanent(format!("asset {asset} clashes")));
            }
        }
        if step == StepName::Translate {
            if let Some(MetaValue::Text(value)) = manifest.meta.get("language") {
                language = value.clone();
            }
            translated = manifest.meta.get("translated") == Some(&MetaValue::Bool(true));
        }
        source.extend(source_fields(step, &manifest.meta));
        if matches!(step, StepName::VideoFetch | StepName::ArxivFetch)
            && let Some(MetaValue::Text(value)) = manifest.meta.get("title")
        {
            // Untrimmed like the title it is compared with.
            original_title = Some(value.clone()).filter(|t| !t.trim().is_empty());
        }
    }
    let note = files
        .get("note")
        .ok_or_else(|| StepFailure::permanent("no note to publish"))?;
    let body = fs::read_to_string(note)?;
    let description = description(&body);
    let title = title.unwrap_or_else(|| job.source.id.clone());
    let note_file = note_file(&title, &job.source.id);
    let rel = vault_path(&job)?;
    let job_url = format!(
        "{}/jobs/{}",
        job.public_url.trim_end_matches('/'),
        job.job_id
    );
    let job_key = pipeline::job_key(job.source.platform, &job.source.id, &job.created);
    let target = vault_dir.join(&rel);
    let tmp = job_dir.join("publish.tmp");
    remove_dir_if_exists(&tmp)?;
    if target.exists() {
        // A crash after rename leaves our folder in place; anything else is never touched.
        if !published_by(&target.join(&note_file), &job_key, &job_url) {
            return Err(StepFailure::permanent(format!("{rel} already exists")));
        }
    } else {
        fs::create_dir(&tmp)?;
        let mut aliases = vec![title.clone()];
        aliases.extend(original_title.filter(|original| *original != title));
        let ids = (job_key.as_str(), job_url.as_str());
        let summary = description.as_deref();
        let fields = frontmatter(&job, &aliases, summary, source, &language, translated, ids);
        let mut links = String::new();
        for (name, label) in COPIED {
            if let Some(src) = files.get(name) {
                fs::copy(src, tmp.join(format!("{name}.md")))?;
                links.push_str(&format!("- [{label}]({name}.md)\n"));
            }
        }
        let mut text = fields + &body;
        if !links.is_empty() {
            // Plain relative links, so any Markdown reader gets from the note to the files
            // beside it.
            text = format!("{}\n\n## 附属文件\n\n{links}", text.trim_end());
        }
        fs::write(tmp.join(&note_file), text)?;
        if !assets.is_empty() {
            fs::create_dir(tmp.join("assets"))?;
            for (asset, src) in &assets {
                fs::copy(src, tmp.join("assets").join(asset))?;
            }
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        // Same filesystem as $DATA/jobs, so the folder appears in the vault atomically.
        fs::rename(&tmp, &target)?;
    }
    let mut manifest = Manifest::default();
    manifest
        .meta
        .insert("vault_path".into(), MetaValue::Text(rel));
    manifest
        .meta
        .insert("note_file".into(), MetaValue::Text(note_file));
    if let Some(description) = description {
        manifest
            .meta
            .insert("description".into(), MetaValue::Text(description));
    }
    pipeline::write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// `<vault>/<domain>/<collection|unsorted>/<YYYY-MM-DD>-<bili|yt|arxiv>-<id>`, dated in +08:00.
fn vault_path(job: &JobSpec) -> Result<String, StepFailure> {
    let created = OffsetDateTime::parse(&job.created, &Rfc3339)
        .map_err(|e| StepFailure::permanent(format!("created {:?}: {e}", job.created)))?;
    let date = created.to_offset(offset!(+8)).date();
    let slug = pipeline::source_slug(job.source.platform, &job.source.id);
    let folder = format!("{date}-{slug}");
    let collection = job.collection.as_deref().unwrap_or("unsorted");
    let parts = [job.vault_name.as_str(), &job.domain, collection, &folder];
    for part in parts {
        if part.is_empty() || part.starts_with('.') || part.contains(['/', '\\']) {
            return Err(StepFailure::permanent(format!("unsafe path part {part:?}")));
        }
    }
    Ok(parts.join("/"))
}

/// `<clean title>.md`, or the source id when nothing is left. A name equal to a file or folder
/// beside it, ignoring case as phone and desktop clients do, gets the suffix " 笔记".
fn note_file(title: &str, source_id: &str) -> String {
    let name = match clean_title(title) {
        name if name.is_empty() => clean_title(source_id),
        name => name,
    };
    let taken = COPIED
        .iter()
        .map(|(n, _)| *n)
        .chain(["assets"])
        .any(|n| n.eq_ignore_ascii_case(&name));
    if taken {
        format!("{name} 笔记.md")
    } else {
        format!("{name}.md")
    }
}

/// NFC; drops `\/:*?"<>|#^[]` and control characters; collapses whitespace; at most 100 UTF-8
/// bytes cut on a character boundary. Leading and trailing dots go too: a leading dot hides
/// the note in Obsidian.
pub(crate) fn clean_title(title: &str) -> String {
    let mut out = String::new();
    let mut gap = false;
    for c in title.nfc() {
        if c.is_whitespace() {
            gap = true;
        } else if !c.is_control() && !"\\/:*?\"<>|#^[]".contains(c) {
            if gap && !out.is_empty() {
                out.push(' ');
            }
            gap = false;
            out.push(c);
        }
    }
    let mut out = out.trim_start_matches(['.', ' ']).to_owned();
    let mut end = out.len().min(TITLE_BYTES);
    while !out.is_char_boundary(end) {
        end -= 1;
    }
    out.truncate(end);
    out.trim_end_matches(['.', ' ']).to_owned()
}

/// Frontmatter fields from what fetch and transcribe found out about the source. A missing or
/// empty value leaves its field out, so older jobs publish as before.
fn source_fields(step: StepName, meta: &BTreeMap<String, MetaValue>) -> Vec<(&'static str, Value)> {
    let text = |key: &str| match meta.get(key) {
        Some(MetaValue::Text(value)) if !value.trim().is_empty() => Some(value.trim()),
        _ => None,
    };
    let fields = match step {
        StepName::VideoFetch => vec![
            ("uploader", text("uploader").map(Value::from)),
            ("published", text("published").map(Value::from)),
            // Whole minutes, rounded; a video under half a minute still counts as one.
            (
                "duration_min",
                match meta.get("duration_ms") {
                    Some(MetaValue::Int(ms)) if *ms > 0 => {
                        Some(json!((ms.saturating_add(30_000) / 60_000).max(1)))
                    }
                    _ => None,
                },
            ),
        ],
        StepName::VideoTranscribe => {
            let transcript = match (text("origin"), text("subtitle_kind")) {
                (Some("whisper"), _) => Some("whisper"),
                (Some("subtitle"), Some(kind @ ("manual" | "ai"))) => Some(kind),
                _ => None,
            };
            vec![("transcript", transcript.map(Value::from))]
        }
        StepName::ArxivFetch => vec![
            (
                "authors",
                match meta.get("authors") {
                    Some(MetaValue::List(names)) if !names.is_empty() => Some(json!(names)),
                    _ => None,
                },
            ),
            ("published", text("published").map(Value::from)),
            ("arxiv_version", text("version").map(Value::from)),
            ("arxiv_category", text("primary_category").map(Value::from)),
        ],
        _ => Vec::new(),
    };
    fields
        .into_iter()
        .filter_map(|(key, value)| Some((key, value?)))
        .collect()
}

/// The note's summary section as one line of plain text, or None when the note has no such
/// section or it is empty. The section ends at the next heading.
fn description(note: &str) -> Option<String> {
    let heading = |line: &str| line.starts_with('#');
    let lines = note
        .lines()
        .skip_while(|line| !heading(line) || line.trim_start_matches('#').trim() != SUMMARY_HEADING)
        .skip(1)
        .take_while(|line| !heading(line))
        .map(|line| {
            let line = line.trim();
            ["> ", "- ", "* ", "+ "]
                .iter()
                .find_map(|marker| line.strip_prefix(marker))
                .unwrap_or(line)
        });
    let text = plain(&lines.collect::<Vec<_>>().join(" "));
    (!text.is_empty()).then_some(text)
}

/// Inline Markdown as the text a reader sees: links keep their label, images go, emphasis and
/// code marks and backslash escapes are dropped, whitespace collapses. `_` stays: in a summary
/// it is far more often part of a name than emphasis.
fn plain(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(c) = rest.chars().next() {
        let image = rest.starts_with("![");
        if let Some((label, after)) = link(&rest[usize::from(image)..]) {
            if !image {
                out.push_str(&plain(label));
            }
            rest = after;
            continue;
        }
        let mut width = c.len_utf8();
        match (c, rest[width..].chars().next()) {
            ('\\', Some(next)) if next.is_ascii_punctuation() => {
                out.push(next);
                width += 1;
            }
            ('*' | '`', _) => {}
            ('~', Some('~')) => width += 1,
            _ => out.push(c),
        }
        rest = &rest[width..];
    }
    out.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `[label](target)` at the start of `text`: the label and the text after the link. A label
/// with a bracket inside is not taken, so `[03:30] … [a](b)` stays two things.
fn link(text: &str) -> Option<(&str, &str)> {
    let close = text.strip_prefix('[')?.find("](")? + 1;
    let label = &text[1..close];
    let end = close + text[close..].find(')')?;
    (!label.contains(['[', ']'])).then(|| (label, &text[end + 1..]))
}

/// YAML frontmatter. Every value is written as JSON, which YAML reads as the same scalar or
/// flow sequence, so titles with quotes or colons need no escaping rules of their own. The first
/// alias is the title.
fn frontmatter(
    job: &JobSpec,
    aliases: &[String],
    description: Option<&str>,
    source: Vec<(&str, Value)>,
    language: &str,
    translated: bool,
    (job_key, job_url): (&str, &str),
) -> String {
    let platform = json!(job.source.platform);
    let mut fields: Vec<(&str, Value)> =
        vec![("title", json!(aliases[0])), ("aliases", json!(aliases))];
    fields.extend(description.map(|text| ("description", json!(text))));
    fields.extend([
        ("source_url", json!(job.source.url)),
        ("platform", platform.clone()),
        ("source_id", json!(job.source.id)),
    ]);
    fields.extend(source);
    fields.extend([
        ("domain", json!(job.domain)),
        ("collection", json!(job.collection.as_deref().unwrap_or(""))),
        ("created", json!(job.created)),
        ("status", json!("unread")),
        ("language", json!(language)),
        ("translated", json!(translated)),
        ("tags", json!(["flori", platform, job.domain])),
        ("job_id", json!(job_key)),
        ("job_url", json!(job_url)),
        ("ai_tool", json!(job.ai.tool)),
        ("ai_model", json!(job.ai.model_note)),
    ]);
    let mut out = String::from("---\n");
    for (key, value) in fields {
        out.push_str(&format!("{key}: {value}\n"));
    }
    out + "---\n\n"
}

/// Whether this job published the note at `path`: its frontmatter `job_id` is the job key. The
/// key does not depend on FLORI_PUBLIC_URL or the integer id, both of which can change under a
/// published note. Notes from before `job_id` have only `job_url` to compare.
fn published_by(path: &Path, job_key: &str, job_url: &str) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    let mut lines = text.lines();
    if lines.next() != Some("---") {
        return false;
    }
    let fields: Vec<&str> = lines.take_while(|line| *line != "---").collect();
    let field = |key: &str| {
        fields
            .iter()
            .find_map(|line| line.strip_prefix(key)?.strip_prefix(": "))
            .and_then(|value| serde_json::from_str::<String>(value).ok())
    };
    match field("job_id") {
        Some(id) => id == job_key,
        None => field("job_url").is_some_and(|url| url == job_url),
    }
}

fn remove_dir_if_exists(dir: &Path) -> io::Result<()> {
    match fs::remove_dir_all(dir) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

#[cfg(test)]
mod tests;
