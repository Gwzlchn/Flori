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

use crate::pipeline::{
    self, JobSpec, Manifest, MetaValue, Platform, StepFailure, StepName, StepResult,
};

/// Logical names copied into the vault as `<name>.md`. The note becomes `<title>.md`.
const COPIED: [&str; 3] = ["transcript", "original", "translation"];
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
            original_title = Some(value.trim().to_owned()).filter(|t| !t.is_empty());
        }
    }
    let note = files
        .get("note")
        .ok_or_else(|| StepFailure::permanent("no note to publish"))?;
    let title = title.unwrap_or_else(|| job.source.id.clone());
    let note_file = note_file(&title, &job.source.id);
    let rel = vault_path(&job)?;
    let job_url = format!(
        "{}/jobs/{}",
        job.public_url.trim_end_matches('/'),
        job.job_id
    );
    let target = vault_dir.join(&rel);
    let tmp = job_dir.join("publish.tmp");
    remove_dir_if_exists(&tmp)?;
    if target.exists() {
        // A crash after rename leaves our folder in place; anything else is never touched.
        if !published_by(&target.join(&note_file), &job_url) {
            return Err(StepFailure::permanent(format!("{rel} already exists")));
        }
    } else {
        fs::create_dir(&tmp)?;
        let mut aliases = vec![title.clone()];
        aliases.extend(original_title.filter(|original| *original != title));
        let fields = frontmatter(&job, &aliases, source, &language, translated, &job_url);
        fs::write(tmp.join(&note_file), fields + &fs::read_to_string(note)?)?;
        for name in COPIED {
            if let Some(src) = files.get(name) {
                fs::copy(src, tmp.join(format!("{name}.md")))?;
            }
        }
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
    pipeline::write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// `<vault>/<domain>/<collection|unsorted>/<YYYY-MM-DD>-<bili|yt|arxiv>-<id>`, dated in +08:00.
fn vault_path(job: &JobSpec) -> Result<String, StepFailure> {
    let created = OffsetDateTime::parse(&job.created, &Rfc3339)
        .map_err(|e| StepFailure::permanent(format!("created {:?}: {e}", job.created)))?;
    let date = created.to_offset(offset!(+8)).date();
    let (prefix, id) = match job.source.platform {
        Platform::Bilibili => ("bili", job.source.id.clone()),
        Platform::Youtube => ("yt", job.source.id.clone()),
        Platform::Arxiv => ("arxiv", job.source.id.replace('/', "_")),
    };
    let folder = format!("{date}-{prefix}-{id}");
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
        .chain(&["assets"])
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

/// YAML frontmatter. Every value is written as JSON, which YAML reads as the same scalar or
/// flow sequence, so titles with quotes or colons need no escaping rules of their own. The first
/// alias is the title.
fn frontmatter(
    job: &JobSpec,
    aliases: &[String],
    source: Vec<(&str, Value)>,
    language: &str,
    translated: bool,
    job_url: &str,
) -> String {
    let platform = json!(job.source.platform);
    let mut fields: Vec<(&str, Value)> = vec![
        ("title", json!(aliases[0])),
        ("aliases", json!(aliases)),
        ("source_url", json!(job.source.url)),
        ("platform", platform.clone()),
        ("source_id", json!(job.source.id)),
    ];
    fields.extend(source);
    fields.extend([
        ("domain", json!(job.domain)),
        ("collection", json!(job.collection.as_deref().unwrap_or(""))),
        ("created", json!(job.created)),
        ("status", json!("unread")),
        ("language", json!(language)),
        ("translated", json!(translated)),
        ("tags", json!(["flori", platform, job.domain])),
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

/// Whether the note at `path` has `job_url` in its frontmatter, i.e. this job published it.
fn published_by(path: &Path, job_url: &str) -> bool {
    let Ok(text) = fs::read_to_string(path) else {
        return false;
    };
    let mut lines = text.lines();
    if lines.next() != Some("---") {
        return false;
    }
    lines
        .take_while(|line| *line != "---")
        .filter_map(|line| line.strip_prefix("job_url: "))
        .any(|value| serde_json::from_str::<String>(value).is_ok_and(|url| url == job_url))
}

fn remove_dir_if_exists(dir: &Path) -> io::Result<()> {
    match fs::remove_dir_all(dir) {
        Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(()),
        other => other,
    }
}

#[cfg(test)]
mod tests;
