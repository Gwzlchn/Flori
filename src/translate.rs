//! Chinese detection, chunking with placeholders, per-chunk cache, validation with one re-ask,
//! and the two output layouts (arXiv translation.md, bilingual video transcript.md). A Chinese
//! video gets the punctuation pass in punctuate.rs instead, over the same chunks and replies.
//! A segment whose reply still fails after the re-ask keeps its original text.
//! Rules: contract 1 "translate" in docs/contracts.md.

mod punctuate;

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    ops::Range,
    path::{Path, PathBuf},
    time::Duration,
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::{
    ai::{self, Env, INPUT_MARKER},
    pipeline::{
        self, JobSpec, Manifest, MetaValue, Pipeline, Section, StepFailure, StepName, StepResult,
    },
};

const PROMPT: &str = include_str!("../prompts/translate.md");
/// Goes before the input marker on the one re-ask after a reply fails validation.
const FEEDBACK_MARKER: &str = "---FLORI-FEEDBACK---";
const CHUNK_CHARS: usize = 12_000;
const MAX_CHUNKS: usize = 25;
const CHUNK_TIMEOUT: Duration = Duration::from_secs(600);
/// Detection reads at most this many characters of translatable text.
const DETECT_CHARS: usize = 20_000;

/// The only language detection: zh, ja, ko, en (Latin letters dominate; arXiv papers are nearly
/// always English), other (Cyrillic and other scripts), or unknown when fewer than 200 letters
/// are comparable. It counts the text that would be translated, so
/// formulas, code, URLs and citations are already placeholders and protected blocks are absent.
/// Chinese: Han / all letters >= 0.3 with kana and hangul under a tenth of the Han count.
fn detect<'a>(texts: impl IntoIterator<Item = &'a str>) -> &'static str {
    let (mut han, mut kana, mut hangul, mut latin, mut other) = (0usize, 0, 0, 0, 0);
    for c in texts.into_iter().flat_map(str::chars).take(DETECT_CHARS) {
        match c {
            '\u{3400}'..='\u{4DBF}'
            | '\u{4E00}'..='\u{9FFF}'
            | '\u{F900}'..='\u{FAFF}'
            | '\u{20000}'..='\u{3134F}' => han += 1,
            '\u{3040}'..='\u{30FF}' | '\u{31F0}'..='\u{31FF}' | '\u{FF66}'..='\u{FF9F}' => {
                kana += 1
            }
            '\u{1100}'..='\u{11FF}' | '\u{3130}'..='\u{318F}' | '\u{AC00}'..='\u{D7AF}' => {
                hangul += 1
            }
            // Latin-1 and Extended-A/B letters; U+D7 and U+F7 are × and ÷.
            'A'..='Z'
            | 'a'..='z'
            | '\u{C0}'..='\u{D6}'
            | '\u{D8}'..='\u{F6}'
            | '\u{F8}'..='\u{24F}'
            | '\u{1E00}'..='\u{1EFF}' => latin += 1,
            c if c.is_alphabetic() => other += 1,
            _ => {}
        }
    }
    let letters = han + kana + hangul + latin + other;
    if letters < 200 {
        "unknown"
    } else if han * 10 >= letters * 3 && (kana + hangul) * 10 <= han {
        "zh"
    } else if kana + hangul > latin + other {
        if kana >= hangul { "ja" } else { "ko" }
    } else if latin >= other {
        "en"
    } else {
        "other"
    }
}

pub async fn run(job_dir: &Path, out_dir: &Path) -> StepResult {
    ai::until_terminated(run_with(job_dir, out_dir, &ai::inherited())).await
}

pub(crate) async fn run_with(job_dir: &Path, out_dir: &Path, env: &Env) -> StepResult {
    let job = pipeline::read_job(job_dir)?;
    let (mut blocks, mut sections, mut subtitle_zh) = (Vec::new(), Vec::new(), None);
    let segments: Vec<Segment> = match job.pipeline {
        Pipeline::Arxiv => {
            let original = pipeline::step_file(job_dir, StepName::ArxivConvert, "original")?;
            blocks = split_blocks(&fs::read_to_string(original)?);
            let texts = blocks.iter().filter(|b| !b.keep).map(|b| &b.text);
            texts.map(|t| protect(t, Pipeline::Arxiv)).collect()
        }
        Pipeline::Video => {
            sections = pipeline::read_sections(job_dir)?;
            subtitle_zh = chinese_subtitle(job_dir, &sections)?;
            let texts = sections.iter().filter(|s| s.has_text()).map(|s| &s.text);
            texts.map(|t| protect(t, Pipeline::Video)).collect()
        }
    };
    let detected = detect(segments.iter().map(|s| s.text.as_str()));
    let language = match job.pipeline {
        Pipeline::Video => transcribed_language(job_dir)?,
        Pipeline::Arxiv => None,
    };
    let language = language.unwrap_or_else(|| detected.to_owned());
    let mut meta = BTreeMap::from([
        ("language".to_owned(), MetaValue::Text(language)),
        ("translated".to_owned(), MetaValue::Bool(false)),
    ]);
    // A Chinese paper stays as it is. A Chinese transcript gets punctuation even when it came
    // with some: neither subtitles nor whisper punctuate reliably.
    let punctuate = detected == "zh" && job.pipeline == Pipeline::Video;
    let limit = if punctuate {
        punctuate::CHUNK_CHARS
    } else {
        CHUNK_CHARS
    };
    let chunks = chunk(&segments, limit);
    // A Chinese subtitle next to a Chinese transcript is no translation of it.
    let subtitle_zh = subtitle_zh.filter(|_| !punctuate);
    let skipped = match detected {
        "zh" if !punctuate => Some("chinese"),
        // A subtitle translation costs no AI call, so neither length nor letter count matters.
        _ if subtitle_zh.is_some() => None,
        "unknown" => Some("unknown"),
        _ if chunks.len() > MAX_CHUNKS => {
            meta.insert("chunks".into(), MetaValue::Int(chunks.len() as i64));
            Some("too_long")
        }
        _ => None,
    };
    let mut manifest = Manifest::default();
    if let Some(reason) = skipped {
        meta.insert("skipped_reason".into(), MetaValue::Text(reason.into()));
    } else if punctuate {
        let replies = ask_all(env, &job, punctuate::PROMPT, &segments, &chunks).await?;
        let (punctuated, tally) = punctuate::apply(&sections, replies);
        for line in &tally.log {
            eprintln!("punctuate {line}");
        }
        let file = "transcript.md";
        fs::write(
            out_dir.join(file),
            crate::video::render_transcript(&punctuated),
        )?;
        manifest.files.insert("transcript".into(), file.into());
        meta.insert("punctuated".into(), MetaValue::Bool(true));
        meta.insert("sections".into(), MetaValue::Int(segments.len() as i64));
        meta.insert("kept_original".into(), MetaValue::Int(tally.kept as i64));
        meta.insert("corrected".into(), MetaValue::Int(tally.corrected as i64));
        meta.insert("reverted".into(), MetaValue::Int(tally.reverted as i64));
    } else {
        let (translated, source) = match subtitle_zh {
            Some(texts) => (texts.into_iter().map(Some).collect(), "subtitle"),
            None => {
                let replies = ask_all(env, &job, PROMPT, &segments, &chunks).await?;
                let kept = replies.iter().filter(|reply| reply.is_err()).count();
                meta.insert("untranslated_blocks".into(), MetaValue::Int(kept as i64));
                (log_kept(replies), "ai")
            }
        };
        let (name, body) = match job.pipeline {
            Pipeline::Arxiv => ("translation", render_arxiv(&blocks, translated)),
            Pipeline::Video => ("transcript", render_video(&sections, translated)),
        };
        let file = format!("{name}.md");
        fs::write(out_dir.join(&file), body)?;
        manifest.files.insert(name.into(), file);
        meta.insert("translated".into(), MetaValue::Bool(true));
        meta.insert("translation_source".into(), MetaValue::Text(source.into()));
    }
    manifest.meta = meta;
    pipeline::write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// The language code video-transcribe recorded (whisper's guess or the subtitle track's).
fn transcribed_language(job_dir: &Path) -> Result<Option<String>, StepFailure> {
    let (_, manifest) = pipeline::prior_step(job_dir, StepName::VideoTranscribe)?;
    Ok(match manifest.meta.get("language") {
        Some(MetaValue::Text(code)) if !code.is_empty() && code != "unknown" => Some(code.clone()),
        _ => None,
    })
}

/// The Chinese manual subtitle as the translation of each section with text, empty where no cue
/// falls. video-transcribe keeps it when the audio told the original language, video-fetch when
/// yt-dlp did. A cue belongs to the 30s window of its start, as transcript text does in
/// video-frames. A cue whose window has no section, or whose section has no text, is dropped:
/// there is no original to pair it with, also before the first section.
fn chinese_subtitle(
    job_dir: &Path,
    sections: &[Section],
) -> Result<Option<Vec<String>>, StepFailure> {
    let mut path = None;
    for step in [StepName::VideoTranscribe, StepName::VideoFetch] {
        let (dir, manifest) = pipeline::prior_step(job_dir, step)?;
        if let Some(file) = manifest.files.get("subtitle_zh") {
            path = Some(dir.join(file));
            break;
        }
    }
    let Some(path) = path else {
        return Ok(None);
    };
    let mut texts = vec![String::new(); sections.len()];
    for cue in crate::video::parse_srt(&fs::read_to_string(path)?) {
        let window = cue.start_ms / pipeline::SECTION_MS * pipeline::SECTION_MS;
        if let Ok(i) = sections.binary_search_by_key(&window, |s| s.start_ms) {
            crate::video::push_text(&mut texts[i], &cue.text);
        }
    }
    let with_text = sections.iter().zip(texts).filter(|(s, _)| s.has_text());
    Ok(Some(with_text.map(|(_, text)| text).collect()))
}

/// A segment's restored reply, or the job.log line saying why it keeps its original text: the
/// chunk, the id in it and what the reply after the re-ask got wrong.
type Reply = Result<String, String>;

/// Replies to `prompt` for every segment, chunk by chunk: translations, or the punctuated
/// transcript.
async fn ask_all(
    env: &Env,
    job: &JobSpec,
    prompt: &str,
    segments: &[Segment],
    chunks: &[Range<usize>],
) -> Result<Vec<Reply>, StepFailure> {
    let cache = env
        .iter()
        .find(|(k, _)| k == "FLORI_CACHE_DIR")
        .map(|(_, v)| PathBuf::from(v))
        .ok_or_else(|| StepFailure::permanent("FLORI_CACHE_DIR is not set"))?;
    fs::create_dir_all(&cache)?;
    let mut replies = Vec::with_capacity(segments.len());
    for (i, range) in chunks.iter().enumerate() {
        let part = &segments[range.clone()];
        let label = format!("chunk {}/{}", i + 1, chunks.len());
        let texts = ask_chunk(env, job, prompt, &cache, part, &label)
            .await
            .map_err(|mut failure| {
                failure.message = format!("{label}: {}", failure.message);
                failure
            })?;
        replies.extend(part.iter().zip(texts).map(|(seg, text)| {
            text.map(|text| seg.restore(&text))
                .map_err(|problem| format!("{label}: {problem}"))
        }));
    }
    Ok(replies)
}

/// The translations, None where a segment keeps its original; each of those gets its line in
/// job.log.
fn log_kept(replies: Vec<Reply>) -> Vec<Option<String>> {
    let log = |line| eprintln!("{line}, original kept");
    replies.into_iter().map(|r| r.map_err(log).ok()).collect()
}

#[derive(Serialize, Deserialize)]
struct Payload {
    segments: Vec<Item>,
}

#[derive(Serialize, Deserialize)]
struct Item {
    id: String,
    text: String,
}

/// Reply texts of one chunk, still holding placeholders, or per segment what is wrong with it.
/// A reply with any problem is asked again once with feedback. When the second reply is unusable
/// as a whole the step fails; otherwise its valid segments are used and the others keep their
/// original. Only replies valid in every segment are cached, under
/// sha256(prompt, model, payload), so a retry only calls the AI for chunks not yet cached; a
/// cached chunk leaves a line in job.log instead of a call record.
async fn ask_chunk(
    env: &Env,
    job: &JobSpec,
    prompt: &str,
    cache: &Path,
    part: &[Segment],
    label: &str,
) -> Result<Vec<Result<String, String>>, StepFailure> {
    let payload = to_payload(part.iter().map(|seg| seg.text.as_str()));
    let model = &job.ai.model_translate;
    let mut hash = Sha256::new();
    for piece in [prompt, model, &payload] {
        hash.update(piece.len().to_le_bytes());
        hash.update(piece);
    }
    let key: String = hash.finalize().iter().map(|b| format!("{b:02x}")).collect();
    let path = cache.join(format!("{key}.json"));
    let cached = fs::read_to_string(&path)
        .ok()
        .and_then(|t| check(&t, part).ok());
    if let Some(texts) = cached.filter(|texts| texts.iter().all(Result::is_ok)) {
        eprintln!("{label}: cached reply {}, no AI call", &key[..12]);
        return Ok(texts);
    }
    let ask = |feedback: String| format!("{prompt}\n{feedback}{INPUT_MARKER}\n{payload}");
    let reply = ai::complete(
        env,
        job.ai.tool,
        model,
        &ask(String::new()),
        &[],
        CHUNK_TIMEOUT,
    )
    .await?;
    let texts = match check(&reply, part) {
        Ok(texts) if texts.iter().all(Result::is_ok) => texts,
        first => {
            let problem = match first {
                Err(problem) => problem,
                Ok(texts) => {
                    let problems: Vec<String> = texts.into_iter().filter_map(Result::err).collect();
                    problems.join("; ")
                }
            };
            let feedback = format!(
                "{FEEDBACK_MARKER}\n上一次输出没有通过校验:{problem}。请修正后重新输出完整 JSON。\n"
            );
            let reply =
                ai::complete(env, job.ai.tool, model, &ask(feedback), &[], CHUNK_TIMEOUT).await?;
            check(&reply, part)
                .map_err(|p| StepFailure::retry(format!("reply failed validation twice: {p}")))?
        }
    };
    if texts.iter().all(Result::is_ok) {
        let tmp = path.with_extension("tmp");
        fs::write(&tmp, to_payload(texts.iter().flatten().map(String::as_str)))?;
        fs::rename(&tmp, &path)?;
    }
    Ok(texts)
}

fn to_payload<'a>(texts: impl Iterator<Item = &'a str>) -> String {
    let segments = texts
        .enumerate()
        .map(|(i, text)| Item {
            id: (i + 1).to_string(),
            text: text.to_owned(),
        })
        .collect();
    serde_json::to_string(&Payload { segments }).expect("strings always serialize")
}

/// A reply usable as a whole is JSON with the chunk's ids in the same order. Then per segment its
/// text, or what is wrong with it: [`check_text`].
fn check(reply: &str, part: &[Segment]) -> Result<Vec<Result<String, String>>, String> {
    let mut body = reply.trim();
    if body.starts_with("```") {
        body = body.split_once('\n').map_or("", |(_, rest)| rest);
        body = body.trim_end().trim_end_matches("```");
    }
    let payload: Payload = serde_json::from_str(body).map_err(|e| format!("不是合法 JSON({e})"))?;
    if payload.segments.len() != part.len() {
        let (want, got) = (part.len(), payload.segments.len());
        return Err(format!("应有 {want} 个 segment,实际 {got} 个"));
    }
    let mut texts = Vec::with_capacity(part.len());
    for (i, (item, seg)) in payload.segments.into_iter().zip(part).enumerate() {
        let id = (i + 1).to_string();
        if item.id != id {
            return Err(format!("第 {id} 个 segment 的 id 应为 \"{id}\""));
        }
        texts.push(check_text(item.text, seg).map_err(|problem| format!("id {id} {problem}")));
    }
    Ok(texts)
}

/// Text that is not empty, holds every placeholder of its segment and no other. A placeholder
/// may come back more than once and restores to the same span each time, as a unit written
/// after both numbers of "from 1.534 to 0.642 $\mu$s".
fn check_text(text: String, seg: &Segment) -> Result<String, String> {
    if text.trim().is_empty() {
        return Err("的 text 为空".into());
    }
    let found: BTreeSet<usize> = pieces(&text)
        .into_iter()
        .filter_map(|p| match p {
            Piece::Mark(n) => Some(n),
            Piece::Text(_) => None,
        })
        .collect();
    let marks = |ns: Vec<usize>| {
        let marks: Vec<String> = ns.into_iter().map(|n| format!("⟦{n}⟧")).collect();
        marks.join(" ")
    };
    let unknown: Vec<usize> = found.range(seg.spans.len()..).copied().collect();
    if !unknown.is_empty() {
        return Err(format!("含有输入里没有的占位符 {}", marks(unknown)));
    }
    let missing: Vec<usize> = (0..seg.spans.len())
        .filter(|n| !found.contains(n))
        .collect();
    if !missing.is_empty() {
        return Err(format!("缺少占位符 {}", marks(missing)));
    }
    Ok(text)
}

/// Translatable text with inline formulas, code, links, images and citations swapped for ⟦n⟧.
#[derive(Debug)]
struct Segment {
    text: String,
    spans: Vec<String>,
}

impl Segment {
    fn restore(&self, translated: &str) -> String {
        pieces(translated)
            .into_iter()
            .map(|p| match p {
                Piece::Text(text) => text,
                Piece::Mark(n) => self.spans[n].as_str(),
            })
            .collect()
    }
}

enum Piece<'a> {
    Text(&'a str),
    Mark(usize),
}

/// Splits at ⟦n⟧ markers. Callers have checked or will check the numbers against the spans.
fn pieces(text: &str) -> Vec<Piece<'_>> {
    let mut out = Vec::new();
    let mut rest = text;
    while let Some(start) = rest.find('⟦') {
        let after = &rest[start + '⟦'.len_utf8()..];
        let digits = after.bytes().take_while(u8::is_ascii_digit).count();
        let mark = after[digits..]
            .starts_with('⟧')
            .then(|| after[..digits].parse::<usize>().ok())
            .flatten();
        let Some(n) = mark else {
            out.push(Piece::Text(&rest[..start + '⟦'.len_utf8()]));
            rest = after;
            continue;
        };
        out.push(Piece::Text(&rest[..start]));
        out.push(Piece::Mark(n));
        rest = &after[digits + '⟧'.len_utf8()..];
    }
    out.push(Piece::Text(rest));
    out
}

fn protect(text: &str, pipeline: Pipeline) -> Segment {
    let (mut out, mut spans) = (String::with_capacity(text.len()), Vec::new());
    let mut rest = text;
    while let Some(c) = rest.chars().next() {
        let len = span_len(rest, pipeline);
        if len == 0 {
            // An escape keeps the next character out of span detection, e.g. `\$`.
            let after = &rest[c.len_utf8()..];
            let escape = pipeline == Pipeline::Arxiv && c == '\\' && !after.starts_with('⟦');
            let next = after.chars().next().filter(|_| escape);
            let n = c.len_utf8() + next.map_or(0, char::len_utf8);
            out.push_str(&rest[..n]);
            rest = &rest[n..];
            continue;
        }
        out.push_str(&format!("⟦{}⟧", spans.len()));
        spans.push(rest[..len].to_owned());
        rest = &rest[len..];
    }
    Segment { text: out, spans }
}

/// Byte length of an inline span that must survive translation verbatim, or 0.
fn span_len(s: &str, pipeline: Pipeline) -> usize {
    // A literal ⟦ in the source would forge a placeholder, so it is a span of its own.
    if s.starts_with('⟦') {
        return '⟦'.len_utf8();
    }
    // Speech has no markup: `$20 ... $200` is money, and the video layout escapes the text.
    if pipeline == Pipeline::Video {
        return 0;
    }
    let closing = |open: usize, close: &str| s[open..].find(close).map(|i| open + i + close.len());
    if s.starts_with('`') {
        let ticks = s.bytes().take_while(|b| *b == b'`').count();
        return closing(ticks, &s[..ticks]).unwrap_or(0);
    }
    if s.starts_with("$$") {
        return closing(2, "$$").unwrap_or(0);
    }
    if s.starts_with('$') {
        // Inline math stays on one line; a stray `$` must not swallow the paragraph.
        let one_line = |end: &usize| *end > 2 && !s[..*end].contains('\n');
        return closing(1, "$").filter(one_line).unwrap_or(0);
    }
    if s.starts_with("![") {
        return s.find("](").and_then(|i| closing(i, ")")).unwrap_or(0);
    }
    if s.starts_with("http://") || s.starts_with("https://") {
        let end = s
            .find(|c: char| c.is_whitespace() || "<>\"')]".contains(c))
            .unwrap_or(s.len());
        return s[..end]
            .trim_end_matches(['.', ',', ';', ':', '!', '?'])
            .len();
    }
    if s.starts_with('[') {
        // Footnote marks such as [^2], and numeric citations such as [12] or [3, 5–7].
        let Some(end) = s.find(']') else { return 0 };
        if footnote_mark(s) > 0 || citation(&s[1..end]) {
            return end + 1;
        }
    }
    if let Some(rest) = s.strip_prefix("\\[") {
        // arxiv-convert escapes every bracket of the paper text, so its citations read \[12\].
        let Some(end) = rest.find("\\]") else {
            return 0;
        };
        if citation(&rest[..end]) {
            return end + 4;
        }
    }
    0
}

/// The inside of a numeric citation such as [12] or [3, 5–7].
fn citation(inner: &str) -> bool {
    inner
        .chars()
        .all(|c| c.is_ascii_digit() || ", -–".contains(c))
        && inner.chars().any(|c| c.is_ascii_digit())
}

/// A unit of original.md, mostly blank-line separated. Kept blocks are copied verbatim: code
/// fences and `$$` math (even across blank lines), tables, image-only paragraphs, list markers
/// with no content, and the References section up to the next heading of the same or a higher
/// level. Footnote definitions are prose wherever they stand; their `[^label]: ` lead stays out
/// of the text so the AI cannot move it.
#[derive(Debug)]
struct Block {
    /// Written verbatim before the text, kept or translated: a footnote definition's `[^3]: `, or
    /// the lone marker lines of the list item this block starts plus its first line's indent.
    lead: String,
    text: String,
    keep: bool,
}

fn split_blocks(md: &str) -> Vec<Block> {
    let lines: Vec<&str> = md.lines().collect();
    let mut out = Vec::new();
    let mut references: Option<usize> = None;
    let leads_at = |i: usize| lines.get(i + 1).is_some_and(|next| leads(lines[i], next));
    let starts_block = |i: usize| {
        let (l, t) = (lines[i], lines[i].trim_start());
        fence(l).is_some() || display(t) || heading(t).is_some() || leads_at(i)
    };
    let mut i = 0;
    while i < lines.len() {
        if lines[i].trim().is_empty() {
            i += 1;
            continue;
        }
        // arxiv-convert writes an item whose first block is not a paragraph as its marker alone
        // on a line, the block directly below. A blank line between them would end the item,
        // so the marker travels with the block and never reaches the AI.
        let mut lead = String::new();
        while leads_at(i) {
            lead.push_str(lines[i]);
            lead.push('\n');
            i += 1;
        }
        let line = lines[i];
        i += 1;
        let first = line.trim_start();
        let mut text = line.to_owned();
        let mut take = |l: &str| {
            text.push('\n');
            text.push_str(l);
        };
        let keep = if let Some(open) = fence(line) {
            while let Some(l) = lines.get(i) {
                take(l);
                i += 1;
                if closes(l, open) {
                    break;
                }
            }
            true
        } else if display(first) {
            let mut closed = first[2..].contains("$$");
            while let Some(l) = lines.get(i).filter(|_| !closed) {
                take(l);
                i += 1;
                closed = l.contains("$$");
            }
            true
        } else if let Some(level) = heading(first) {
            if references.is_some_and(|r| level <= r) {
                references = None;
            }
            let title = first[level..].trim_start_matches(|c: char| !c.is_alphabetic());
            let title = title.trim().to_lowercase();
            if references.is_none() && ["references", "bibliography", "参考文献"].contains(&&*title)
            {
                references = Some(level);
            }
            references.is_some()
        } else {
            // A table or a line of images is kept, and in a list the next item follows it
            // directly: the block ends at its last line of the same kind.
            let kind = |l: &str| (l.trim_start().starts_with('|'), images_only(l));
            let joins = |l: &str| kind(first) == (false, false) || kind(l) == kind(first);
            while let Some(l) = lines
                .get(i)
                .filter(|l| !l.trim().is_empty() && !starts_block(i) && joins(l))
            {
                take(l);
                i += 1;
            }
            let all = |p: &str| text.lines().all(|l| l.trim_start().starts_with(p));
            let reference = references.is_some() && footnote_lead(&text) == 0;
            let markers = text.lines().all(|l| marker_column(l).is_some());
            reference || markers || all("|") || all("![")
        };
        // The first line's indent goes with the markers: a reply that drops leading spaces must
        // not move the block out of its item.
        let indent = if lead.is_empty() {
            0
        } else {
            line.len() - first.len()
        };
        let note = if keep {
            0
        } else {
            footnote_lead(&text[indent..])
        };
        lead.push_str(&text[..indent + note]);
        out.push(Block {
            lead,
            text: text[indent + note..].to_owned(),
            keep,
        });
    }
    out
}

/// The content column of a line holding nothing but list markers, or None. `4. -` is item 4
/// whose first block is a list, and that list's first item starts with a block. Markers are
/// CommonMark's: `-`, `+`, `*`, or 1 to 9 digits and `.` or `)`, each followed by a space or the
/// end of the line. The last item begins with a blank line, so its content column is one past
/// its marker. `- - -` and `* * *` are thematic breaks, not markers.
fn marker_column(line: &str) -> Option<usize> {
    let mut end = None;
    loop {
        let at = end.unwrap_or(0);
        let rest = line[at..].trim_start_matches(' ');
        if rest.is_empty() {
            return end.map(|e| e + 1);
        }
        let ink: Vec<char> = rest.chars().filter(|c| *c != ' ').collect();
        if ink.len() >= 3 && "-*_".contains(ink[0]) && ink.iter().all(|c| *c == ink[0]) {
            return None;
        }
        let width = if rest.starts_with(['-', '+', '*']) {
            1
        } else {
            let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
            let closed = rest[digits..].starts_with(['.', ')']);
            if !(1..=9).contains(&digits) || !closed {
                return None;
            }
            digits + 1
        };
        let after = &rest[width..];
        if !after.is_empty() && !after.starts_with(' ') {
            return None;
        }
        end = Some(line.len() - after.len());
    }
}

/// Whether `line` holds only list markers and `next` starts their item's content: not blank and
/// indented to the content column. A shallower line means CommonMark ends the item empty.
fn leads(line: &str, next: &str) -> bool {
    let indent = next.len() - next.trim_start_matches(' ').len();
    marker_column(line).is_some_and(|column| !next.trim().is_empty() && indent >= column)
}

/// A line of nothing but images, such as a figure's panels.
fn images_only(line: &str) -> bool {
    let (mut rest, mut images) = (line.trim(), 0);
    while rest.starts_with("![") {
        let len = span_len(rest, Pipeline::Arxiv);
        if len == 0 {
            return false;
        }
        rest = rest[len..].trim_start();
        images += 1;
    }
    images > 0 && rest.is_empty()
}

/// Byte length of a footnote mark `[^label]` at the start of `s`, or 0.
fn footnote_mark(s: &str) -> usize {
    let label = s
        .strip_prefix("[^")
        .and_then(|r| r.split_once(']'))
        .map_or("", |(l, _)| l);
    let ok = !label.is_empty()
        && label
            .chars()
            .all(|c| c.is_alphanumeric() || "-_".contains(c));
    if ok { label.len() + 3 } else { 0 }
}

/// Byte length of a footnote definition's leading `[^label]: `, or 0.
fn footnote_lead(text: &str) -> usize {
    let mark = footnote_mark(text);
    if mark > 0 && text[mark..].starts_with(": ") {
        mark + 2
    } else {
        0
    }
}

/// An opening code fence: 3 or more backticks or tildes, and a backtick fence's info string has
/// no backtick. Any indent is accepted, because CommonMark counts it from the list item's content
/// column and arxiv-convert indents fences in item 10 and later, or in nested lists, by 4 or more.
/// Returns the character, the run length and the indent.
fn fence(line: &str) -> Option<(char, usize, usize)> {
    let rest = line.trim_start_matches(' ');
    let c = rest.chars().next().filter(|c| *c == '`' || *c == '~')?;
    let n = rest.chars().take_while(|x| *x == c).count();
    let info_ok = c == '~' || !rest[n..].contains('`');
    (n >= 3 && info_ok).then_some((c, n, line.len() - rest.len()))
}

/// A line that closes a fence opened with `n` of `c` at `indent`: at most 3 spaces deeper than the
/// opening, then at least `n` of the same character and nothing else but whitespace.
fn closes(line: &str, (c, n, indent): (char, usize, usize)) -> bool {
    let rest = line.trim_start_matches(' ');
    let run = rest.trim_end();
    line.len() - rest.len() <= indent + 3 && run.chars().all(|x| x == c) && run.chars().count() >= n
}

/// A `$$` line that opens display math. `$$x$$ is small` is inline math inside a paragraph.
fn display(line: &str) -> bool {
    let rest = line.trim_end().strip_prefix("$$");
    rest.is_some_and(|rest| rest.ends_with("$$") || !rest.contains("$$"))
}

fn heading(line: &str) -> Option<usize> {
    let level = line.bytes().take_while(|b| *b == b'#').count();
    ((1..=6).contains(&level) && line[level..].starts_with(' ')).then_some(level)
}

/// Consecutive segments grouped up to `limit` characters. A single longer segment is its own chunk.
fn chunk(segments: &[Segment], limit: usize) -> Vec<Range<usize>> {
    let (mut chunks, mut start, mut size) = (Vec::new(), 0, 0);
    for (i, seg) in segments.iter().enumerate() {
        let len = seg.text.chars().count();
        if i > start && size + len > limit {
            chunks.push(start..i);
            (start, size) = (i, 0);
        }
        size += len;
    }
    if start < segments.len() {
        chunks.push(start..segments.len());
    }
    chunks
}

/// Translated blocks in place of the others; None keeps a block's original text.
fn render_arxiv(blocks: &[Block], translated: Vec<Option<String>>) -> String {
    let mut translated = translated.into_iter();
    let parts: Vec<String> = blocks
        .iter()
        .map(|b| {
            let text = if b.keep {
                None
            } else {
                translated.next().flatten()
            };
            let text = text.unwrap_or_else(|| b.text.clone());
            b.lead.clone() + &text
        })
        .collect();
    parts.join("\n\n") + "\n"
}

/// Per section: time heading, key frames, Chinese, then the original folded in a callout. Both
/// texts are escaped like video-frames' transcript.md, so speech never turns into markup.
/// A section without Chinese, None or empty, shows its original alone.
fn render_video(sections: &[Section], translated: Vec<Option<String>>) -> String {
    let mut translated = translated.into_iter().map(Option::unwrap_or_default);
    let mut out = String::new();
    for section in sections {
        out.push_str(&format!("## {}\n\n", pipeline::clock(section.start_ms)));
        for frame in &section.frames {
            out.push_str(&format!("![](assets/{frame})\n\n"));
        }
        if section.has_text() {
            let chinese = translated.next().unwrap_or_default();
            let original = pipeline::escape_markdown(section.text.trim());
            if chinese.trim().is_empty() {
                // A section the Chinese subtitle leaves out keeps only its original.
                out.push_str(&format!("{original}\n\n"));
                continue;
            }
            out.push_str(&pipeline::escape_markdown(chinese.trim()));
            out.push_str("\n\n> [!quote]- 原文\n");
            for line in original.lines() {
                out.push_str(&format!("> {line}\n"));
            }
            out.push('\n');
        }
    }
    out.truncate(out.trim_end().len());
    out + "\n"
}

#[cfg(test)]
mod tests;
