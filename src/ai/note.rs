//! The note step: the source text and up to 10 images in one AI call. The model places images
//! with `![描述](img:N)`; the note on disk links `assets/<basename>` instead, which publish
//! resolves because it merges every step's assets.

use std::{
    fmt::Write as _,
    fs, io,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use tokio::process::Command;

use super::{Env, INPUT_MARKER, Scratch, complete, inherited, media_type, until_terminated};
use crate::pipeline::{self, Manifest, Pipeline, Section, StepName, StepResult};

const NOTE_TIMEOUT: Duration = Duration::from_secs(1800);
const NOTE_ARXIV: &str = include_str!("../../prompts/note-arxiv.md");
const NOTE_VIDEO: &str = include_str!("../../prompts/note-video.md");
/// Appended only when images go along.
const NOTE_IMAGES: &str = include_str!("../../prompts/note-images.md");
const MAX_IMAGES: usize = 10;
/// How much of a frame's section text, or of a figure caption, the image list shows.
const FRAME_TEXT_CHARS: usize = 150;
const CAPTION_CHARS: usize = 300;
/// Larger images are shrunk for the model; the vault keeps linking the original.
const MAX_IMAGE_BYTES: u64 = 1 << 20;
const MAX_IMAGE_SIDE: u32 = 1568;
const SHRINK_TIMEOUT: Duration = Duration::from_secs(120);

/// An image the note may show.
#[derive(Debug)]
struct Picture {
    /// Name under the source step's `assets/`, which the note links.
    basename: String,
    /// The file sent to the model: the asset itself or a shrunk copy.
    path: PathBuf,
    /// Its line in the image list: a time and section text, or a caption.
    label: String,
}

pub async fn note(job_dir: &Path, out_dir: &Path) -> StepResult {
    until_terminated(note_with(job_dir, out_dir, &inherited())).await
}

pub(crate) async fn note_with(job_dir: &Path, out_dir: &Path, env: &Env) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let (template, fetch, text, pictures) = match job.pipeline {
        Pipeline::Arxiv => {
            let (dir, _) = pipeline::prior_step(job_dir, StepName::ArxivConvert)?;
            let original = fs::read_to_string(pipeline::step_file(
                job_dir,
                StepName::ArxivConvert,
                "original",
            )?)?;
            let figures = paper_figures(&original, &dir.join("assets"));
            (NOTE_ARXIV, StepName::ArxivFetch, original, figures)
        }
        // Sections carry the transcript without image links, with times the note can cite.
        Pipeline::Video => {
            let (dir, _) = pipeline::prior_step(job_dir, StepName::VideoFrames)?;
            let sections = pipeline::read_sections(job_dir)?;
            let lines: Vec<String> = sections
                .iter()
                .filter(|s| s.has_text())
                .map(|s| format!("[{}] {}", pipeline::clock(s.start_ms), s.text.trim()))
                .collect();
            let frames = video_frames(&sections, &dir.join("assets"));
            (NOTE_VIDEO, StepName::VideoFetch, lines.join("\n"), frames)
        }
    };
    let title = pipeline::prior_step(job_dir, fetch)?.1.title;
    let title = title.unwrap_or_else(|| job.source.id.clone());
    let scratch = Scratch::new()?;
    let pictures = sendable(env, pictures, &scratch.0).await;
    let mut prompt = template.to_owned();
    let mut list = String::new();
    if !pictures.is_empty() {
        prompt.push_str(NOTE_IMAGES);
        list.push_str("图片(序号与消息末尾附图前的 [N] 一一对应):\n");
        for (i, picture) in pictures.iter().enumerate() {
            let _ = writeln!(list, "[{}] {}", i + 1, picture.label);
        }
        list.push('\n');
    }
    let prompt = format!("{prompt}\n{INPUT_MARKER}\n标题:{title}\n\n{list}{text}");
    let images: Vec<PathBuf> = pictures.iter().map(|p| p.path.clone()).collect();
    let reply = complete(
        env,
        job.ai.tool,
        &job.ai.model_note,
        &prompt,
        &images,
        NOTE_TIMEOUT,
    )
    .await?;
    let mut note = link_images(&reply, &pictures);
    if !note.ends_with('\n') {
        note.push('\n');
    }
    fs::write(out_dir.join("note.md"), note)?;
    let mut manifest = Manifest::default();
    manifest.files.insert("note".into(), "note.md".into());
    pipeline::write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// Keyframes spread evenly over the video's time, each with its time and the start of its
/// section's text. Frame names carry their time (`frame-<ms>.jpg`).
fn video_frames(sections: &[Section], dir: &Path) -> Vec<Picture> {
    let mut frames = Vec::new();
    for section in sections {
        for name in &section.frames {
            let ms = name
                .strip_prefix("frame-")
                .and_then(|rest| rest.split('.').next())
                .and_then(|ms| ms.parse().ok())
                .unwrap_or(section.start_ms);
            if let Some(path) = usable(dir, name) {
                let text: String = section.text.trim().chars().take(FRAME_TEXT_CHARS).collect();
                let label = format!("{} {text}", pipeline::clock(ms));
                frames.push((
                    ms,
                    Picture {
                        basename: name.clone(),
                        path,
                        label,
                    },
                ));
            }
        }
    }
    spread(frames, MAX_IMAGES)
}

/// Images of original.md that have a caption: a paragraph of `![](assets/<name>)` images
/// followed by a paragraph that starts like "Figure 2", "Table 1" or "图 3". Each image of a
/// subfigure group gets the group's caption. Spread evenly over the paper.
fn paper_figures(original: &str, dir: &Path) -> Vec<Picture> {
    let paragraphs: Vec<&str> = original
        .split("\n\n")
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .collect();
    let mut figures: Vec<(u64, Picture)> = Vec::new();
    for pair in paragraphs.windows(2) {
        if !is_caption(pair[1]) {
            continue;
        }
        let caption: String = pair[1].chars().take(CAPTION_CHARS).collect();
        for name in asset_links(pair[0]) {
            let seen = figures.iter().any(|(_, f)| f.basename == name);
            if let Some(path) = usable(dir, name).filter(|_| !seen) {
                let position = figures.len() as u64;
                let label = caption.clone();
                figures.push((
                    position,
                    Picture {
                        basename: name.to_owned(),
                        path,
                        label,
                    },
                ));
            }
        }
    }
    spread(figures, MAX_IMAGES)
}

/// Basenames of the `![…](assets/<name>)` links in a paragraph.
fn asset_links(paragraph: &str) -> Vec<&str> {
    paragraph
        .split("](assets/")
        .skip(1)
        .filter_map(|rest| rest.split_once(')').map(|(name, _)| name))
        .filter(|name| !name.is_empty() && !name.contains(['/', '\\']))
        .collect()
}

fn is_caption(paragraph: &str) -> bool {
    let text = paragraph
        .trim_start_matches(['*', '_', '\\', ' '])
        .to_lowercase();
    ["figure", "fig.", "fig ", "table", "图", "表"]
        .iter()
        .any(|word| text.starts_with(word))
}

/// The asset's path when it exists and its format can be sent; SVG and the like cannot.
fn usable(dir: &Path, name: &str) -> Option<PathBuf> {
    let path = dir.join(name);
    (media_type(&path).is_some() && path.is_file()).then_some(path)
}

/// At most `k` items spread evenly over their keys (time or position), first and last included.
fn spread(mut items: Vec<(u64, Picture)>, k: usize) -> Vec<Picture> {
    items.sort_by_key(|(key, _)| *key);
    if items.len() > k && k > 1 {
        let (first, last) = (items[0].0, items[items.len() - 1].0);
        let mut chosen: Vec<usize> = Vec::with_capacity(k);
        for i in 0..k as u64 {
            let target = first + (last - first) * i / (k as u64 - 1);
            let nearest = (0..items.len())
                .filter(|j| !chosen.contains(j))
                .min_by_key(|&j| items[j].0.abs_diff(target));
            chosen.extend(nearest);
        }
        let mut index = 0;
        items.retain(|_| {
            index += 1;
            chosen.contains(&(index - 1))
        });
    }
    items.into_iter().map(|(_, picture)| picture).collect()
}

/// Pictures ready to send. One that is too large is shrunk by ffmpeg into `scratch`; one that
/// cannot be read or shrunk is left out, since a note without it is still a note.
async fn sendable(env: &Env, pictures: Vec<Picture>, scratch: &Path) -> Vec<Picture> {
    let mut out = Vec::with_capacity(pictures.len());
    for (i, mut picture) in pictures.into_iter().enumerate() {
        let shrunk = match too_large(&picture.path) {
            Ok(false) => Ok(None),
            Ok(true) => shrink(env, &picture.path, scratch, i).await.map(Some),
            Err(e) => Err(e),
        };
        match shrunk {
            Ok(None) => out.push(picture),
            Ok(Some(path)) => {
                picture.path = path;
                out.push(picture);
            }
            Err(e) => eprintln!("note: leaving out {}: {e}", picture.basename),
        }
    }
    out
}

/// Over 1 MiB, or a long side over 1568px when the header tells.
fn too_large(path: &Path) -> io::Result<bool> {
    if fs::metadata(path)?.len() > MAX_IMAGE_BYTES {
        return Ok(true);
    }
    Ok(image_size(&fs::read(path)?).is_some_and(|(w, h)| w.max(h) > MAX_IMAGE_SIDE))
}

/// Width and height from a PNG, GIF or JPEG header; None for other formats.
fn image_size(bytes: &[u8]) -> Option<(u32, u32)> {
    let be16 = |i: usize| {
        Some(u32::from(u16::from_be_bytes(
            bytes.get(i..i + 2)?.try_into().ok()?,
        )))
    };
    let be32 = |i: usize| Some(u32::from_be_bytes(bytes.get(i..i + 4)?.try_into().ok()?));
    let le16 = |i: usize| {
        Some(u32::from(u16::from_le_bytes(
            bytes.get(i..i + 2)?.try_into().ok()?,
        )))
    };
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return Some((be32(16)?, be32(20)?));
    }
    if bytes.starts_with(b"GIF8") {
        return Some((le16(6)?, le16(8)?));
    }
    if !bytes.starts_with(&[0xFF, 0xD8]) {
        return None;
    }
    // JPEG: walk the segments to the first start-of-frame marker.
    let mut i = 2;
    while *bytes.get(i)? == 0xFF {
        let marker = *bytes.get(i + 1)?;
        if marker == 0xFF {
            // Fill byte before a marker.
            i += 1;
            continue;
        }
        let start_of_frame =
            (0xC0..=0xCF).contains(&marker) && ![0xC4, 0xC8, 0xCC].contains(&marker);
        if start_of_frame {
            return Some((be16(i + 7)?, be16(i + 5)?));
        }
        i += 2 + usize::try_from(be16(i + 2)?).ok()?;
    }
    None
}

/// A copy within 1568x1568 for the model only. PNG stays PNG so transparency stays readable;
/// everything else becomes JPEG.
async fn shrink(env: &Env, source: &Path, scratch: &Path, n: usize) -> io::Result<PathBuf> {
    let png = media_type(source) == Some("image/png");
    let target = scratch.join(format!("image-{n}.{}", if png { "png" } else { "jpg" }));
    let scale = format!(
        "scale='min({MAX_IMAGE_SIDE},iw)':'min({MAX_IMAGE_SIDE},ih)':force_original_aspect_ratio=decrease"
    );
    let mut cmd = Command::new("ffmpeg");
    cmd.args(["-nostdin", "-loglevel", "error", "-y", "-i"])
        .arg(source)
        .args(["-vf", &scale, "-frames:v", "1"])
        .arg(&target)
        .env_clear()
        .envs(env.iter().map(|(k, v)| (k, v)))
        .stdin(Stdio::null())
        .kill_on_drop(true);
    let status = tokio::time::timeout(SHRINK_TIMEOUT, cmd.status())
        .await
        .map_err(|_| io::Error::other("ffmpeg timed out"))??;
    if !status.success() || !target.is_file() {
        return Err(io::Error::other(format!("ffmpeg failed ({status})")));
    }
    Ok(target)
}

/// Replaces each `![描述](img:N)` with `![描述](assets/<basename>)` when N names a sent image and
/// drops the whole reference otherwise. Other images and links stay as they are.
fn link_images(note: &str, pictures: &[Picture]) -> String {
    let mut out = String::with_capacity(note.len());
    let mut rest = note;
    while let Some(start) = rest.find("![") {
        out.push_str(&rest[..start]);
        rest = &rest[start..];
        let Some((alt, n, len)) = image_reference(rest) else {
            out.push_str("![");
            rest = &rest[2..];
            continue;
        };
        if let Some(picture) = n.checked_sub(1).and_then(|i| pictures.get(i)) {
            let _ = write!(out, "![{}](assets/{})", clean_alt(alt), picture.basename);
        }
        rest = &rest[len..];
    }
    out.push_str(rest);
    out
}

/// `![alt](img:N …)` at the start of `s`, on one line: the alt text, N (0 when missing) and the
/// byte length. Anything after N up to `)`, such as a title, is ignored.
fn image_reference(s: &str) -> Option<(&str, usize, usize)> {
    let line = s.split('\n').next()?;
    let close = line.find("](")?;
    let target = line[close..].strip_prefix("](img:")?;
    let end = target.find(')')?;
    let digits: String = target[..end]
        .chars()
        .take_while(char::is_ascii_digit)
        .collect();
    let n = digits.parse().unwrap_or(0);
    Some((&line[2..close], n, close + "](img:".len() + end + 1))
}

/// Alt text that cannot end the link early or turn into Obsidian syntax: no brackets, no
/// backslash, and no `|`, which Obsidian reads as an image size.
fn clean_alt(alt: &str) -> String {
    let kept: String = alt
        .chars()
        .filter(|c| !matches!(c, '[' | ']' | '\\'))
        .map(|c| if c == '|' { '/' } else { c })
        .collect();
    kept.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests;
