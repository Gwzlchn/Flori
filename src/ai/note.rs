//! The note step: the source text and every video keyframe or paper figure in one AI call, as
//! many as one request takes. The model places images with `![描述](img:N)`; the note on
//! disk links `assets/<basename>` instead, which publish resolves because it merges every step's
//! assets.

use std::{
    fmt::Write as _,
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};

use serde::Deserialize;
use tokio::process::Command;

use super::{Env, INPUT_MARKER, Image, Scratch, complete, inherited, media_type, until_terminated};
use crate::pipeline::{self, Manifest, Pipeline, Section, StepFailure, StepName, StepResult};

const NOTE_TIMEOUT: Duration = Duration::from_secs(1800);
const NOTE_ARXIV: &str = include_str!("../../prompts/note-arxiv.md");
const NOTE_VIDEO: &str = include_str!("../../prompts/note-video.md");
/// Appended only when images go along.
const NOTE_IMAGES: &str = include_str!("../../prompts/note-images.md");
/// A video sends every keyframe and a paper every figure, so each key point can get
/// its image; video-frames bounds frames per section. Only one request's limits bound them here:
/// the API's image count, and the base64 bytes of the images. The API takes at most 32 MB per
/// request, prompt included, and base64 adds a third, so 20 MiB leaves room for the longest text.
const MAX_REQUEST_IMAGES: usize = 600;
const REQUEST_IMAGE_BYTES: u64 = 20 << 20;
/// How much of a frame's section text, or of a figure's label and caption, the image list shows.
const FRAME_TEXT_CHARS: usize = 150;
const CAPTION_CHARS: usize = 300;
/// Larger images are shrunk for the model; the vault keeps linking the original. Frames go as
/// small copies so a long video costs per minute, not per pixel of its source; figures keep up
/// to the model's standard resolution, since their text is small.
const MAX_IMAGE_BYTES: u64 = 1 << 20;
const FRAME_SIDE: u32 = 768;
const FIGURE_SIDE: u32 = 1568;
const SHRINK_TIMEOUT: Duration = Duration::from_secs(120);

/// An image the note may show.
#[derive(Debug)]
struct Picture {
    /// Name under the source step's `assets/`, which the note links.
    basename: String,
    /// The asset itself, which the call record names.
    asset: PathBuf,
    /// The file sent to the model: the asset itself or a shrunk copy.
    path: PathBuf,
    /// Its line in the image list: a time and section text, or a caption.
    label: String,
}

/// One entry of arxiv-convert's figures.json: a figure element of the page, in document order.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Figure {
    /// "Figure 2", or empty.
    label: String,
    caption: String,
    /// Asset basenames of all its images, nested panels included, in page order.
    images: Vec<String>,
}

pub async fn note(job_dir: &Path, out_dir: &Path) -> StepResult {
    until_terminated(note_with(job_dir, out_dir, &inherited())).await
}

pub(crate) async fn note_with(job_dir: &Path, out_dir: &Path, env: &Env) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let (template, fetch, text, pictures, side) = match job.pipeline {
        Pipeline::Arxiv => {
            let (dir, converted) = pipeline::prior_step(job_dir, StepName::ArxivConvert)?;
            let original = fs::read_to_string(pipeline::step_file(
                job_dir,
                StepName::ArxivConvert,
                "original",
            )?)?;
            let figures = paper_figures(&dir, &converted)?;
            (
                NOTE_ARXIV,
                StepName::ArxivFetch,
                original,
                figures,
                FIGURE_SIDE,
            )
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
            let text = lines.join("\n");
            (NOTE_VIDEO, StepName::VideoFetch, text, frames, FRAME_SIDE)
        }
    };
    let title = pipeline::prior_step(job_dir, fetch)?.1.title;
    let title = title.unwrap_or_else(|| job.source.id.clone());
    let scratch = Scratch::new()?;
    let pictures = sendable(env, pictures, side, &scratch.0).await;
    let pictures = within_budget(pictures, REQUEST_IMAGE_BYTES);
    let mut prompt = template.to_owned();
    let mut list = String::new();
    if !pictures.is_empty() {
        prompt.push_str(NOTE_IMAGES);
        list.push_str("图片(序号即消息末尾附图的顺序):\n");
        for (i, picture) in pictures.iter().enumerate() {
            let _ = writeln!(list, "[{}] {}", i + 1, picture.label);
        }
        list.push('\n');
    }
    let prompt = format!("{prompt}\n{INPUT_MARKER}\n标题:{title}\n\n{list}{text}");
    let images: Vec<Image> = pictures
        .iter()
        .map(|p| Image {
            file: p.path.clone(),
            shows: p.asset.clone(),
        })
        .collect();
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

/// Every keyframe in time order, each with its time and the start of its section's text. Frame names carry their time (`frame-<ms>.jpg`).
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
                        asset: path.clone(),
                        path,
                        label,
                    },
                ));
            }
        }
    }
    spread(frames, MAX_REQUEST_IMAGES)
}

/// The images of figures.json in document order, every one with its figure's label and caption.
/// Only assets arxiv-convert lists are taken, each once. A paper converted before figures.json
/// existed sends none.
fn paper_figures(dir: &Path, converted: &Manifest) -> Result<Vec<Picture>, StepFailure> {
    let Some(file) = converted.files.get("figures") else {
        return Ok(Vec::new());
    };
    let figures: Vec<Figure> =
        pipeline::read_json(&dir.join(file)).map_err(|e| StepFailure::permanent(e.to_string()))?;
    let mut pictures: Vec<(u64, Picture)> = Vec::new();
    for figure in figures {
        let label = [figure.label.trim(), figure.caption.trim()]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(": ");
        let label: String = label.chars().take(CAPTION_CHARS).collect();
        for name in figure.images {
            let seen = pictures.iter().any(|(_, p)| p.basename == name);
            if seen || !converted.assets.contains(&name) {
                continue;
            }
            if let Some(path) = usable(&dir.join("assets"), &name) {
                let position = pictures.len() as u64;
                pictures.push((
                    position,
                    Picture {
                        basename: name,
                        asset: path.clone(),
                        path,
                        label: label.clone(),
                    },
                ));
            }
        }
    }
    Ok(spread(pictures, MAX_REQUEST_IMAGES))
}

/// The asset's path when it is a file whose bytes are an image the CLIs take; SVG, or an HTML
/// page saved under an image name, is not.
fn usable(dir: &Path, name: &str) -> Option<PathBuf> {
    let path = dir.join(name);
    head(&path).ok().and_then(|h| media_type(&h)).map(|_| path)
}

/// The first bytes of a file, enough for [`media_type`].
fn head(path: &Path) -> io::Result<Vec<u8>> {
    let mut head = Vec::with_capacity(16);
    fs::File::open(path)?.take(16).read_to_end(&mut head)?;
    Ok(head)
}

/// At most `k` items spread evenly over their keys (time or position), first and last included.
fn spread(mut items: Vec<(u64, Picture)>, k: usize) -> Vec<Picture> {
    items.sort_by_key(|(key, _)| *key);
    let keys: Vec<u64> = items.iter().map(|(key, _)| *key).collect();
    let mut taken = evenly(&keys, k).into_iter();
    items.retain(|_| taken.next().unwrap_or(false));
    items.into_iter().map(|(_, picture)| picture).collect()
}

/// Which of the sorted `keys` to keep so that at most `k` remain, spread evenly over the keys:
/// the first and the last, then those nearest to equal steps between them. One keeps the first.
fn evenly(keys: &[u64], k: usize) -> Vec<bool> {
    if keys.len() <= k {
        return vec![true; keys.len()];
    }
    let mut taken = vec![false; keys.len()];
    if k == 1 {
        taken[0] = true;
    } else if k > 1 {
        let (first, last) = (keys[0], keys[keys.len() - 1]);
        for i in 0..k as u64 {
            let target = first + (last - first) * i / (k as u64 - 1);
            let nearest = (0..keys.len())
                .filter(|&j| !taken[j])
                .min_by_key(|&j| keys[j].abs_diff(target));
            if let Some(j) = nearest {
                taken[j] = true;
            }
        }
    }
    taken
}

/// The pictures whose files, base64-encoded, fit in `budget` bytes. When they all do not, as many
/// as fit are kept spread evenly over their positions, first and last included. The files are
/// the ones sent, after shrinking.
fn within_budget(pictures: Vec<Picture>, budget: u64) -> Vec<Picture> {
    let sizes: Vec<u64> = pictures
        .iter()
        .map(|p| {
            let bytes = fs::metadata(&p.path).map_or(MAX_IMAGE_BYTES, |m| m.len());
            bytes.div_ceil(3) * 4
        })
        .collect();
    let total: u64 = sizes.iter().sum();
    if total <= budget {
        return pictures;
    }
    let positions: Vec<u64> = (0..pictures.len() as u64).collect();
    // Start from the share the budget allows on average and give up one picture at a time.
    let mut k = usize::try_from(budget * pictures.len() as u64 / total).unwrap_or(0);
    let taken = loop {
        let taken = evenly(&positions, k);
        let size: u64 = sizes
            .iter()
            .zip(&taken)
            .filter(|(_, t)| **t)
            .map(|(s, _)| s)
            .sum();
        if size <= budget || k == 0 {
            break taken;
        }
        k -= 1;
    };
    eprintln!(
        "note: {} images are over the {} MiB request budget, sending {k} spread over the source",
        pictures.len(),
        budget >> 20
    );
    pictures
        .into_iter()
        .zip(taken)
        .filter_map(|(picture, taken)| taken.then_some(picture))
        .collect()
}

/// Pictures ready to send. One that is too large for `side` is shrunk by ffmpeg into `scratch`; one that
/// cannot be read or shrunk, or is still too large afterwards, is left out, since a note without
/// it is still a note and a rejected image would fail the whole call.
async fn sendable(env: &Env, pictures: Vec<Picture>, side: u32, scratch: &Path) -> Vec<Picture> {
    let mut out = Vec::with_capacity(pictures.len());
    for (i, mut picture) in pictures.into_iter().enumerate() {
        let shrunk = match too_large(&picture.path, side) {
            Ok(false) => Ok(None),
            Ok(true) => match shrink(env, &picture.path, side, scratch, i).await {
                Ok(copy) if too_large(&copy, side).unwrap_or(true) => {
                    Err(io::Error::other("still too large after shrinking"))
                }
                result => result.map(Some),
            },
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

/// Over 1 MiB, or a long side over `side` when the header tells: WebP is judged by bytes only.
fn too_large(path: &Path, side: u32) -> io::Result<bool> {
    if fs::metadata(path)?.len() > MAX_IMAGE_BYTES {
        return Ok(true);
    }
    Ok(image_size(&fs::read(path)?).is_some_and(|(w, h)| w.max(h) > side))
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

/// A copy within `side` x `side` for the model only. PNG stays PNG so transparency stays readable;
/// everything else becomes JPEG. The image bytes come from the paper, so ffmpeg reads them with
/// a demuxer fixed by their real type, may only open local files, and gets no credentials: of
/// the step's environment only PATH, LANG and TMPDIR.
async fn shrink(
    env: &Env,
    source: &Path,
    side: u32,
    scratch: &Path,
    n: usize,
) -> io::Result<PathBuf> {
    let demuxer = match media_type(&head(source)?) {
        Some("image/png") => "png_pipe",
        Some("image/jpeg") => "jpeg_pipe",
        Some("image/gif") => "gif",
        Some("image/webp") => "webp_pipe",
        _ => return Err(io::Error::other("not an image ffmpeg should read")),
    };
    let extension = if demuxer == "png_pipe" { "png" } else { "jpg" };
    let target = scratch.join(format!("image-{n}.{extension}"));
    let scale =
        format!("scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease");
    let allowed = env
        .iter()
        .filter(|(k, _)| ["PATH", "LANG", "TMPDIR"].iter().any(|name| k == name));
    let mut cmd = Command::new("ffmpeg");
    cmd.args([
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-protocol_whitelist",
        "file",
    ])
    .args(["-f", demuxer, "-i"])
    .arg(source)
    .args(["-vf", &scale, "-frames:v", "1"])
    .arg(&target)
    .env_clear()
    .envs(allowed.map(|(k, v)| (k, v)))
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
/// drops the whole reference otherwise. Images already under `assets/` stay; any other image,
/// such as an `http`, `data:` or local path the model was talked into writing, is dropped: the
/// vault is append-only and every client would load it.
fn link_images(note: &str, pictures: &[Picture]) -> String {
    let mut out = String::with_capacity(note.len());
    let mut rest = note;
    while let Some(start) = rest.find("![") {
        out.push_str(&rest[..start]);
        rest = &rest[start..];
        let Some((alt, target, len)) = image_link(rest) else {
            out.push_str("![");
            rest = &rest[2..];
            continue;
        };
        if let Some(n) = target.strip_prefix("img:") {
            let digits: String = n.chars().take_while(char::is_ascii_digit).collect();
            let index = digits.parse::<usize>().ok().and_then(|n| n.checked_sub(1));
            if let Some(picture) = index.and_then(|i| pictures.get(i)) {
                let _ = write!(out, "![{}](assets/{})", clean_alt(alt), picture.basename);
            }
        } else if target.starts_with("assets/") && !target.contains("..") {
            out.push_str(&rest[..len]);
        }
        rest = &rest[len..];
    }
    out.push_str(rest);
    out
}

/// `![alt](target)` at the start of `s`, on one line: the alt text, the target up to the first
/// `)` and the byte length.
fn image_link(s: &str) -> Option<(&str, &str, usize)> {
    let line = s.split('\n').next()?;
    let close = line.find("](")?;
    let target_start = close + "](".len();
    let end = target_start + line[target_start..].find(')')?;
    Some((&line[2..close], &line[target_start..end], end + 1))
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
