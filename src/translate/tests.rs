use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    path::PathBuf,
};

use super::*;
use crate::{
    ai::tests::{Fixture, job_spec},
    pipeline::{AiTool, StepExit, escape_markdown},
    prompts::{self, builtin},
};

fn fixture_text(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/translate")
        .join(name);
    fs::read_to_string(path).unwrap()
}

fn arxiv_segments(md: &str) -> Vec<Segment> {
    split_blocks(md)
        .iter()
        .filter(|b| !b.keep)
        .map(|b| protect(&b.text, Pipeline::Arxiv))
        .collect()
}

/// Detection exactly as each path runs it: arXiv through blocks and placeholders, a transcript
/// line by line as video sections.
fn detect_as(pipeline: Pipeline, text: &str) -> &'static str {
    let segments: Vec<Segment> = match pipeline {
        Pipeline::Arxiv => arxiv_segments(text),
        Pipeline::Video => text.lines().map(|l| protect(l, Pipeline::Video)).collect(),
    };
    detect(segments.iter().map(|s| s.text.as_str()))
}

#[test]
fn detection_samples() {
    // The seven samples of the translation research, then extra edge cases.
    let cases = [
        ("lang-en.md", Pipeline::Arxiv, "en"),
        // An English paper quoting a few Chinese examples is still English (Han ratio 0.09).
        ("lang-en-zh.md", Pipeline::Arxiv, "en"),
        ("lang-zh.md", Pipeline::Arxiv, "zh"),
        // Spoken Chinese full of English terms, as a transcript.
        ("lang-spoken-mixed.md", Pipeline::Video, "zh"),
        ("lang-ja.md", Pipeline::Arxiv, "ja"),
        ("lang-ko.md", Pipeline::Arxiv, "ko"),
        ("lang-short.md", Pipeline::Arxiv, "unknown"),
        ("lang-mixed.md", Pipeline::Arxiv, "zh"),
        // Formulas and code would outvote the Chinese text if they were counted.
        ("lang-formula.md", Pipeline::Arxiv, "zh"),
        // So would the URLs.
        ("lang-urls.md", Pipeline::Arxiv, "zh"),
        // Latin letters count as en, other scripts do not.
        ("lang-fr.md", Pipeline::Arxiv, "en"),
        ("lang-ru.md", Pipeline::Arxiv, "other"),
    ];
    for (name, pipeline, want) in cases {
        assert_eq!(detect_as(pipeline, &fixture_text(name)), want, "{name}");
    }
    // × and ÷ are not letters: a Chinese note full of 8×8×8 shapes stays Chinese.
    for sign in ['×', '÷'] {
        let shapes = format!("8{sign}8{sign}8{sign}8 ").repeat(270);
        let note = format!("{}\n\n输入尺寸 {shapes}\n", fixture_text("lang-zh.md"));
        assert_eq!(detect_as(Pipeline::Arxiv, &note), "zh", "{sign}");
    }
}

#[test]
fn stripping_matters_for_formula_and_url_samples() {
    // Counting the raw text, the latin letters of code, formulas and URLs win.
    for name in ["lang-formula.md", "lang-urls.md"] {
        assert_ne!(detect([fixture_text(name).as_str()]), "zh", "{name}");
    }
}

#[test]
fn blocks_keep_math_code_tables_images_and_references_whole() {
    let paper = fixture_text("paper.md");
    let blocks = split_blocks(&paper);
    let kept: Vec<&str> = blocks
        .iter()
        .filter(|b| b.keep)
        .map(|b| b.text.as_str())
        .collect();
    assert_eq!(kept.len(), 7, "{kept:#?}");
    assert!(kept[0].starts_with("$$\n") && kept[0].ends_with("$$") && kept[0].contains("\n\nG(x)"));
    assert!(
        kept[1].starts_with("```python")
            && kept[1].contains("\n\n    logits")
            && kept[1].ends_with("```")
    );
    assert!(kept[2].starts_with("| Model") && kept[2].lines().count() == 4);
    assert_eq!(kept[3], "![](assets/fig1.png)");
    assert_eq!(
        &kept[4..],
        [
            "## References",
            "[1] A. Author. Outrageously large neural networks. 2017.",
            "[2] B. Author. Switch transformers. 2021."
        ]
    );
    // The appendix heading closes the References section.
    let texts: Vec<&str> = blocks
        .iter()
        .filter(|b| !b.keep)
        .map(|b| b.text.as_str())
        .collect();
    assert_eq!(texts.len(), 6);
    assert_eq!(texts[4], "## A Training details");
    // Nothing protected leaks into what is sent, whatever the chunk size.
    let segments = arxiv_segments(&paper);
    for limit in [1, 200, CHUNK_CHARS] {
        let chunks = chunk(&segments, limit);
        assert_eq!(
            chunks.iter().map(|r| r.len()).sum::<usize>(),
            segments.len()
        );
        for seg in &segments {
            for marker in ["$$", "```", "| Model", "[1] A. Author", "fig1.png"] {
                assert!(!seg.text.contains(marker), "{marker} in {:?}", seg.text);
            }
        }
    }
    assert_eq!(chunk(&segments, 1).len(), segments.len());
    assert_eq!(chunk(&segments, CHUNK_CHARS).len(), 1);
}

#[test]
fn footnotes_are_translated_without_their_lead() {
    // arxiv-convert writes definitions before the References; other sources may put them last.
    let md = "## 1 Intro\n\nCode is public[^1].\n\n[^1]: All code is at https://example.org/gan.\n\n\
              ## References\n\n[1] A. Author. GANs. 2014.\n\n[^note-2]: Seen after the references.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str, &str)> = blocks
        .iter()
        .map(|b| (b.keep, b.lead.as_str(), b.text.as_str()))
        .collect();
    assert_eq!(
        kinds,
        [
            (false, "", "## 1 Intro"),
            (false, "", "Code is public[^1]."),
            (false, "[^1]: ", "All code is at https://example.org/gan."),
            (true, "", "## References"),
            (true, "", "[1] A. Author. GANs. 2014."),
            (false, "[^note-2]: ", "Seen after the references."),
        ]
    );
    // The AI only sees the definition's text, never its `[^1]: ` lead.
    let note = protect(&blocks[2].text, Pipeline::Arxiv);
    assert_eq!(note.text, "All code is at ⟦0⟧.");
    // A reply with a full-width colon and the URL moved to the end still renders the lead first.
    let reply = r#"{"segments":[{"id":"1","text":"：全部代码见下方链接 ⟦0⟧"}]}"#;
    let checked = check(reply, std::slice::from_ref(&note)).unwrap();
    let translated = note.restore(checked[0].as_ref().unwrap());
    let only_note = [Block {
        lead: blocks[2].lead.clone(),
        text: blocks[2].text.clone(),
        keep: false,
    }];
    assert_eq!(
        render_arxiv(&only_note, vec![Some(translated)]),
        "[^1]: ：全部代码见下方链接 https://example.org/gan\n"
    );
    // References in the body are placeholders, and dropping one fails validation.
    let body = [protect(&blocks[1].text, Pipeline::Arxiv)];
    assert_eq!(body[0].text, "Code is public⟦0⟧.");
    let dropped = r#"{"segments":[{"id":"1","text":"代码已公开。"}]}"#;
    let problem = check(dropped, &body).unwrap().remove(0).unwrap_err();
    assert_eq!(problem, "id 1 缺少占位符 ⟦0⟧");
}

#[tokio::test]
async fn footnote_leads_survive_a_full_round_trip() {
    let md = format!(
        "{}\n\n[^1]: The note text keeps its place[^2].\n\n[^2]: Second note.\n",
        paragraph(1, "")
    );
    let fx = arxiv(&md);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fs::read_to_string(out.join("translation.md")).unwrap(), md);
}

#[test]
fn fences_close_only_on_a_matching_fence_line() {
    let md = "Intro text.\n\n\
              ````markdown\n```python\nprint(1)\n```\n\n<img src=x onerror=alert(1)>\n````\n\n\
              Middle text.\n\n\
              ~~~\n```\n~~ not yet\n~~~~ \n\n\
              - item\n\n  ```text\n  inside a list\n\n  ```\n\n\
              Outro text.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "Intro text."),
            (
                true,
                "````markdown\n```python\nprint(1)\n```\n\n<img src=x onerror=alert(1)>\n````"
            ),
            (false, "Middle text."),
            (true, "~~~\n```\n~~ not yet\n~~~~ "),
            (false, "- item"),
            (true, "  ```text\n  inside a list\n\n  ```"),
            (false, "Outro text."),
        ]
    );
}

#[test]
fn fences_in_numbered_and_nested_lists_stay_whole() {
    // arxiv-convert's list output: continuation lines are indented by the marker's width.
    let tenth = "10. Tenth item.\n    \n    ```\n    x = 1\n    \n    y = 2\n    ```";
    let nested = "- Outer.\n  \n  - Inner.\n    \n    ```\n    for x in xs:\n    \n        print(x)\n    ```";
    let md = format!("{tenth}\n\n{nested}\n\nAfter the lists.\n");
    let blocks = split_blocks(&md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "10. Tenth item."),
            (true, "    ```\n    x = 1\n    \n    y = 2\n    ```"),
            (false, "- Outer."),
            (false, "  - Inner."),
            (
                true,
                "    ```\n    for x in xs:\n    \n        print(x)\n    ```"
            ),
            (false, "After the lists."),
        ]
    );
}

/// A line holding nothing but list markers by CommonMark's rules: `-`, `+`, `*`, or 1 to 9
/// digits and `.` or `)`. Written apart from the code under test, as the tests' own oracle.
fn lone_marker(line: &str) -> bool {
    // Byte slicing would panic inside a Chinese word, so the suffix is stripped as a char.
    let ordered = |w: &str| {
        w.strip_suffix(['.', ')']).is_some_and(|digits| {
            (1..=9).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit())
        })
    };
    let words: Vec<&str> = line.split_whitespace().collect();
    !words.is_empty()
        && words
            .iter()
            .all(|w| ["-", "+", "*"].contains(w) || ordered(w))
}

/// Every lone marker line is followed directly by its item's first line, indented past the last
/// marker: a blank line or a shallower line there would leave the item empty.
fn assert_markers_attached(md: &str) {
    let lines: Vec<&str> = md.lines().collect();
    for (i, line) in lines.iter().enumerate() {
        if lone_marker(line) {
            let next = lines.get(i + 1).copied().unwrap_or("");
            let indent = next.len() - next.trim_start().len();
            assert!(
                !next.trim().is_empty() && indent > line.trim_end().len(),
                "line {} {line:?} is followed by {next:?}",
                i + 1
            );
        }
    }
}

fn kinds(blocks: &[Block]) -> Vec<(bool, &str, &str)> {
    blocks
        .iter()
        .map(|b| (b.keep, b.lead.as_str(), b.text.as_str()))
        .collect()
}

/// lists.md is arxiv-convert's real output: an item whose first block is not a paragraph has its
/// marker alone on a line, and the block follows on the next line at the item's content column.
#[test]
fn a_lone_list_marker_leads_the_block_its_item_starts_with() {
    let blocks = split_blocks(&fixture_text("lists.md"));
    assert_eq!(
        kinds(&blocks),
        [
            (false, "", "# A Twelve Step Recipe"),
            (false, "", "## 1 Method"),
            (false, "", "The procedure has twelve steps:"),
            (false, "", "1. Step 1 prepares the data."),
            (
                true,
                "2.\n   ",
                "```\n   def step(batch):\n   \n       return model(batch)\n   ```"
            ),
            (
                true,
                "3.\n   ",
                "| Split | Size |\n   | --- | --- |\n   | train | 50k |"
            ),
            (
                true,
                "4. -\n     ",
                "```\n     for x in xs:\n         print(x)\n     ```"
            ),
            (false, "", "   - The inner loop logs each sample."),
            (false, "", "   Item four ends here."),
            (
                false,
                "5.\n   ",
                "- Clean the text.\n   - Tokenize it.\n6. Step 6 prepares the data.\n\
                 7. Step 7 prepares the data.\n8. Step 8 prepares the data.\n\
                 9. Step 9 prepares the data."
            ),
            (
                true,
                "10.\n    ",
                "```\n    def step(batch):\n    \n        return model(batch)\n    ```"
            ),
            (
                true,
                "11.\n    ",
                "| Split | Size |\n    | --- | --- |\n    | train | 50k |"
            ),
            (true, "12.\n    ", "$$x=1$$"),
            (false, "", "After the list we report results."),
            (true, "-\n  ", "```\n  make train\n  ```"),
            (false, "", "- Second bullet."),
            (false, "", "  - Inner one."),
            (true, "  -\n    ", "```\n    inner code\n    ```"),
            (false, "", "The end."),
        ]
    );
}

#[test]
fn other_markers_lead_too_and_a_table_ends_at_its_last_row() {
    let md = "*\n  ```\n  a\n  ```\n\n+\n  | a |\n  | - |\n+ Next.\n\n\
              7)\n   $$y$$\n\n\
              1. Text first.\n   \n   | b |\n   | - |\n2. Second.\n";
    assert_eq!(
        kinds(&split_blocks(md)),
        [
            (true, "*\n  ", "```\n  a\n  ```"),
            (true, "+\n  ", "| a |\n  | - |"),
            (false, "", "+ Next."),
            (true, "7)\n   ", "$$y$$"),
            (false, "", "1. Text first."),
            (true, "", "   | b |\n   | - |"),
            (false, "", "2. Second."),
        ]
    );
}

#[test]
fn marker_columns_follow_commonmark() {
    let cases = [
        ("-", Some(2)),
        ("+ ", Some(2)),
        ("  *", Some(4)),
        ("1.", Some(3)),
        ("9)", Some(3)),
        ("10.", Some(4)),
        ("123456789.", Some(11)),
        ("4. -", Some(5)),
        ("2.  ", Some(3)),
        ("1234567890.", None),
        ("1.5", None),
        ("-1.", None),
        ("10.x", None),
        ("#", None),
        ("", None),
        ("   ", None),
        ("---", None),
        ("- - -", None),
        ("4. * * *", None),
    ];
    for (line, want) in cases {
        assert_eq!(marker_column(line), want, "{line:?}");
    }
    assert!(leads("10.", "    ```"));
    assert!(!leads("10.", "   ```"));
    assert!(!leads("10.", "    "));
    assert!(!leads("2. ", "3. Next."));
}

#[test]
fn marker_only_blocks_are_kept() {
    // Empty items, and a marker whose next line sits left of the item's content column, so
    // CommonMark ends the item there and the marker leads nothing.
    let md = "Intro.\n\n-\n\n1. \n2. \n\n10.\n   ```\n   x = 1\n   ```\n\nEnd.\n";
    assert_eq!(
        kinds(&split_blocks(md)),
        [
            (false, "", "Intro."),
            (true, "", "-"),
            (true, "", "1. \n2. "),
            (true, "", "10."),
            (true, "", "   ```\n   x = 1\n   ```"),
            (false, "", "End."),
        ]
    );
}

#[test]
fn consecutive_marker_lines_all_lead_the_block() {
    // arxiv-convert's output for an enumerate item whose first block is an itemize inside an
    // itemize that starts with code: two marker lines stand above the fence.
    let md = "1. Step one.\n2.\n   - -\n       ```\n       make all\n       ```\n\
              \x20  - Inner text.\n3. Step three.\n";
    let blocks = split_blocks(md);
    assert_eq!(
        kinds(&blocks),
        [
            (false, "", "1. Step one."),
            (
                true,
                "2.\n   - -\n       ",
                "```\n       make all\n       ```"
            ),
            (false, "", "   - Inner text.\n3. Step three."),
        ]
    );
    let translated = vec![
        Some("1. 第一步。".into()),
        Some("   - 内层文字。\n3. 第三步。".into()),
    ];
    let out = render_arxiv(&blocks, translated);
    assert_eq!(
        out,
        "1. 第一步。\n\n2.\n   - -\n       ```\n       make all\n       ```\n\n\
         \x20  - 内层文字。\n3. 第三步。\n"
    );
    assert_markers_attached(&out);
}

#[test]
fn an_image_item_ends_at_its_last_image_line() {
    // arxiv-convert's output for items holding only a figure: the next item follows directly.
    // Sent along with it, the image would be a placeholder the AI may move into that item.
    let md = "1. Step one.\n2.\n   ![](assets/x.png)\n3. Step three.\n\
              4.\n   ![](assets/x.png) ![](assets/y.png)\n5. \n6. \n\n\
              -\n  ![](assets/z.png) A photo of the setup.\n- Next.\n";
    assert_eq!(
        kinds(&split_blocks(md)),
        [
            (false, "", "1. Step one."),
            (true, "2.\n   ", "![](assets/x.png)"),
            (false, "", "3. Step three."),
            (true, "4.\n   ", "![](assets/x.png) ![](assets/y.png)"),
            (true, "", "5. \n6. "),
            // Text after an image on its line is a paragraph, sent as before.
            (
                false,
                "-\n  ",
                "![](assets/z.png) A photo of the setup.\n- Next."
            ),
        ]
    );
}

#[test]
fn list_items_starting_with_text_split_as_before() {
    let items: Vec<String> = (1..=11).map(|n| format!("{n}. Item {n} text.")).collect();
    let md = format!(
        "{}\n    \n    Second paragraph of eleven.\n    \n    ```\n    x = 1\n    ```\n\
         12. Twelve.\n13. \n14. Fourteen.\n\n- Outer.\n  \n  - Inner.\n- Last.\n",
        items.join("\n")
    );
    let blocks = split_blocks(&md);
    let first = items.join("\n");
    assert_eq!(
        kinds(&blocks),
        [
            (false, "", first.as_str()),
            (false, "", "    Second paragraph of eleven."),
            (true, "", "    ```\n    x = 1\n    ```"),
            (false, "", "12. Twelve.\n13. \n14. Fourteen."),
            (false, "", "- Outer."),
            (false, "", "  - Inner.\n- Last."),
        ]
    );
}

#[test]
fn a_lone_marker_renders_directly_above_its_block() {
    let blocks = split_blocks(&fixture_text("lists.md"));
    // A stand-in translation that keeps every mark and space but visibly changes the words.
    let translated = blocks
        .iter()
        .filter(|b| !b.keep)
        .map(|b| Some(b.text.to_uppercase()))
        .collect();
    let md = render_arxiv(&blocks, translated);
    let want = concat!(
        "# A TWELVE STEP RECIPE\n\n## 1 METHOD\n\nTHE PROCEDURE HAS TWELVE STEPS:\n\n",
        "1. STEP 1 PREPARES THE DATA.\n\n",
        "2.\n   ```\n   def step(batch):\n   \n       return model(batch)\n   ```\n\n",
        "3.\n   | Split | Size |\n   | --- | --- |\n   | train | 50k |\n\n",
        "4. -\n     ```\n     for x in xs:\n         print(x)\n     ```\n\n",
        "   - THE INNER LOOP LOGS EACH SAMPLE.\n\n   ITEM FOUR ENDS HERE.\n\n",
        "5.\n   - CLEAN THE TEXT.\n   - TOKENIZE IT.\n6. STEP 6 PREPARES THE DATA.\n",
        "7. STEP 7 PREPARES THE DATA.\n8. STEP 8 PREPARES THE DATA.\n",
        "9. STEP 9 PREPARES THE DATA.\n\n",
        "10.\n    ```\n    def step(batch):\n    \n        return model(batch)\n    ```\n\n",
        "11.\n    | Split | Size |\n    | --- | --- |\n    | train | 50k |\n\n",
        "12.\n    $$x=1$$\n\n",
        "AFTER THE LIST WE REPORT RESULTS.\n\n",
        "-\n  ```\n  make train\n  ```\n\n- SECOND BULLET.\n\n",
        "  - INNER ONE.\n\n  -\n    ```\n    inner code\n    ```\n\n",
        "THE END.\n",
    );
    assert_eq!(md, want);
    assert_markers_attached(&md);
}

#[tokio::test]
async fn list_markers_and_their_blocks_never_reach_the_ai() {
    let original = fixture_text("lists.md");
    let fx = arxiv(&original);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    let prompt = calls[0]["prompt"].as_str().unwrap();
    let (_, input) = prompt.rsplit_once(INPUT_MARKER).unwrap();
    let payload: Payload = serde_json::from_str(input.trim()).unwrap();
    let sent: Vec<&str> = payload.segments.iter().map(|s| s.text.as_str()).collect();
    // An item that starts with text is sent as before, marker and all.
    assert!(sent.contains(&"1. Step 1 prepares the data."), "{sent:#?}");
    for text in &sent {
        assert!(!text.lines().all(lone_marker), "marker only: {text:?}");
        assert!(!text.lines().any(lone_marker), "lone marker in {text:?}");
        for code in [
            "```",
            "|",
            "$$",
            "batch",
            "print",
            "make train",
            "inner code",
        ] {
            assert!(!text.contains(code), "{code} sent in {text:?}");
        }
    }
    let translation = fs::read_to_string(out.join("translation.md")).unwrap();
    assert_markers_attached(&translation);
    // The echo gives back every line; only blank lines between blocks may differ.
    let filled = |md: &str| -> Vec<String> {
        let lines = md.lines().filter(|l| !l.trim().is_empty());
        lines.map(str::to_owned).collect()
    };
    assert_eq!(filled(&translation), filled(&original));
}

#[test]
fn fence_open_and_close_edge_cases() {
    assert_eq!(fence("```py`x"), None);
    assert_eq!(fence("~~~ a`b"), Some(('~', 3, 0)));
    // Two backticks are inline code, not a fence.
    assert_eq!(fence("``"), None);
    assert_eq!(fence("``x``"), None);
    let open = fence("  ```").unwrap();
    assert_eq!(open, ('`', 3, 2));
    // A line with an info string opens nothing and closes nothing.
    assert!(!closes("``` x", open));
    // At most 3 spaces deeper than the opening fence.
    assert!(closes("     ```", open));
    assert!(!closes("      ```", open));
    assert!(closes("```` ", open));
    assert!(!closes("~~~", open));
    // The same rules inside split_blocks: the fence ends only at the plain closing line.
    let md = "```\na\n``` x\n\n       ```\nb\n```\n\nAfter.\n";
    let kinds: Vec<(bool, String)> = split_blocks(md)
        .into_iter()
        .map(|b| (b.keep, b.text))
        .collect();
    assert_eq!(
        kinds,
        [
            (true, "```\na\n``` x\n\n       ```\nb\n```".to_owned()),
            (false, "After.".to_owned()),
        ]
    );
}

#[test]
fn display_math_only_opens_on_its_own() {
    let md = "$$x$$ is small and\nthe loss is $y$.\n\n$$ a\n\nb $$\n\n$$z$$\n\nEnd $5\nand $6.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "$$x$$ is small and\nthe loss is $y$."),
            (true, "$$ a\n\nb $$"),
            (true, "$$z$$"),
            (false, "End $5\nand $6."),
        ]
    );
    assert_eq!(
        protect(&blocks[0].text, Pipeline::Arxiv).spans,
        ["$$x$$", "$y$"]
    );
    assert!(protect(&blocks[3].text, Pipeline::Arxiv).spans.is_empty());
}

#[test]
fn inline_spans_become_placeholders_and_come_back() {
    let text = "We study $f(x)$ with `top_k`, see https://example.org/moe. and [3, 5–7]; \
                ![fig](assets/a.png) costs \\$5 [not a cite].";
    let seg = protect(text, Pipeline::Arxiv);
    assert_eq!(
        seg.text,
        "We study ⟦0⟧ with ⟦1⟧, see ⟦2⟧. and ⟦3⟧; ⟦4⟧ costs \\$5 [not a cite]."
    );
    assert_eq!(
        seg.spans,
        [
            "$f(x)$",
            "`top_k`",
            "https://example.org/moe",
            "[3, 5–7]",
            "![fig](assets/a.png)"
        ]
    );
    assert_eq!(seg.restore(&seg.text), text);
    // Translations may move placeholders around.
    assert_eq!(
        seg.restore("⟦4⟧⟦3⟧⟦2⟧⟦1⟧⟦0⟧"),
        "![fig](assets/a.png)[3, 5–7]https://example.org/moe`top_k`$f(x)$"
    );
}

#[test]
fn escaped_citations_from_arxiv_convert_are_placeholders() {
    let text =
        "As in \\[11, 7\\] and \\[3, 5–7\\], not \\[x\\] or \\!\\[a\\](https://e.org/p.png).";
    let seg = protect(text, Pipeline::Arxiv);
    assert_eq!(
        seg.text,
        "As in ⟦0⟧ and ⟦1⟧, not \\[x\\] or \\!\\[a\\](⟦2⟧)."
    );
    assert_eq!(
        seg.spans,
        ["\\[11, 7\\]", "\\[3, 5–7\\]", "https://e.org/p.png"]
    );
    assert_eq!(seg.restore(&seg.text), text);
}

#[test]
fn speech_gets_no_placeholders() {
    let text = "ChatGPT Plus costs $20 a month and Pro costs $200 a month, see `docs` [3].";
    let seg = protect(text, Pipeline::Video);
    assert!(seg.spans.is_empty());
    assert_eq!(seg.text, text);
}

/// The echo the fake CLI gives: every segment comes back unchanged.
fn round_trip(text: &str, pipeline: Pipeline) -> String {
    let part = [protect(text, pipeline)];
    let reply = to_payload(part.iter().map(|s| s.text.as_str()));
    part[0].restore(check(&reply, &part).unwrap()[0].as_ref().unwrap())
}

#[test]
fn literal_placeholder_brackets_cannot_forge_a_placeholder() {
    for pipeline in [Pipeline::Arxiv, Pipeline::Video] {
        for text in ["Use ⟦0⟧ and `code` here ⟦1⟧.", "Only ⟦0⟧.", "\\⟦0⟧ ⟦⟦9⟧"]
        {
            assert_eq!(round_trip(text, pipeline), text, "{pipeline:?} {text}");
        }
    }
    let seg = protect("Only ⟦0⟧.", Pipeline::Video);
    assert_eq!(seg.spans, ["⟦"]);
}

#[test]
fn a_reply_unusable_as_a_whole_is_rejected_with_feedback() {
    let part = [
        protect("Use `x` here.", Pipeline::Arxiv),
        protect("Plain.", Pipeline::Arxiv),
    ];
    let ok = r#"{"segments":[{"id":"1","text":"这里用 ⟦0⟧。"},{"id":"2","text":"简单。"}]}"#;
    let texts = [Ok("这里用 ⟦0⟧。".to_owned()), Ok("简单。".to_owned())];
    assert_eq!(check(ok, &part).unwrap(), texts);
    assert_eq!(check(&format!("```json\n{ok}\n```"), &part).unwrap(), texts);
    let cases = [
        ("not json", "JSON"),
        (r#"{"segments":[{"id":"1","text":"⟦0⟧"}]}"#, "应有 2 个"),
        (
            r#"{"segments":[{"id":"2","text":"⟦0⟧"},{"id":"1","text":"a"}]}"#,
            "id 应为",
        ),
    ];
    for (reply, needle) in cases {
        let problem = check(reply, &part).unwrap_err();
        assert!(problem.contains(needle), "{reply}: {problem}");
    }
}

/// The sentence of chunk 14 that failed job 30: the reply wrote the unit after both numbers.
const MICROSECONDS: &str = "For one QP and 16 KiB messages, spraying increases goodput from \
    92.2 to 323.5 Gb/s, a $3.51\\times$ improvement, while reducing mean network completion from \
    1.534 to 0.642 $\\mu$s (58.2%). The 16 KiB workload remains at 323.5 Gb/s with two and four \
    QPs, exceeding the corresponding fixed-path results by $2.81\\times$.";

#[test]
fn a_placeholder_may_repeat_but_none_may_be_missing_or_unknown() {
    let seg = protect(MICROSECONDS, Pipeline::Arxiv);
    assert_eq!(seg.spans, ["$3.51\\times$", "$\\mu$", "$2.81\\times$"]);
    let part = std::slice::from_ref(&seg);
    let reply = |text: &str| to_payload([text].into_iter());
    let repeated = "对于一个 QP 和 16 KiB 消息,包喷洒将吞吐从 92.2 Gb/s 提高到 323.5 Gb/s,\
        即 ⟦0⟧ 的提升,同时将平均网络完成时间从 1.534 ⟦1⟧s 降至 0.642 ⟦1⟧s(58.2%)。\
        16 KiB 工作负载在两个和四个 QP 下仍保持 323.5 Gb/s,比相应的固定路径结果高出 ⟦2⟧。";
    let checked = check(&reply(repeated), part).unwrap();
    assert_eq!(checked, [Ok(repeated.to_owned())]);
    // Each repeat restores to the same original.
    assert!(
        seg.restore(repeated)
            .contains("从 1.534 $\\mu$s 降至 0.642 $\\mu$s"),
        "{}",
        seg.restore(repeated)
    );
    let problems = [
        ("从 1.534 降至 0.642 ⟦1⟧s,提升 ⟦0⟧。", "id 1 缺少占位符 ⟦2⟧"),
        (
            "提升 ⟦0⟧ ⟦1⟧ ⟦2⟧ ⟦3⟧ ⟦12⟧。",
            "id 1 含有输入里没有的占位符 ⟦3⟧ ⟦12⟧",
        ),
        ("只有中文。", "id 1 缺少占位符 ⟦0⟧ ⟦1⟧ ⟦2⟧"),
        (" ", "id 1 的 text 为空"),
    ];
    for (text, want) in problems {
        let checked = check(&reply(text), part).unwrap();
        assert_eq!(checked, [Err(want.to_owned())], "{text}");
    }
    // A segment with a problem leaves the others of its chunk usable.
    let part = [
        protect("Use `x` here.", Pipeline::Arxiv),
        protect("Plain.", Pipeline::Arxiv),
    ];
    let reply = r#"{"segments":[{"id":"1","text":"没有占位符"},{"id":"2","text":"简单。"}]}"#;
    assert_eq!(
        check(reply, &part).unwrap(),
        [
            Err("id 1 缺少占位符 ⟦0⟧".to_owned()),
            Ok("简单。".to_owned())
        ]
    );
}

fn arxiv(original: &str) -> Fixture {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", original)],
        &[],
        &[],
    );
    fx
}

/// A video job whose video-transcribe recorded `language` and whose sections are given.
fn video(sections: &serde_json::Value, language: &str) -> Fixture {
    video_with_subtitle(sections, language, None)
}

/// The same, with video-fetch's Chinese manual subtitle when `subtitle_zh` is given.
fn video_with_subtitle(
    sections: &serde_json::Value,
    language: &str,
    subtitle_zh: Option<&str>,
) -> Fixture {
    video_with_subtitle_from(sections, language, subtitle_zh, StepName::VideoFetch)
}

/// The same, with the Chinese manual subtitle kept by `holder`: video-fetch when yt-dlp told
/// the original language, video-transcribe when the audio did.
fn video_with_subtitle_from(
    sections: &serde_json::Value,
    language: &str,
    subtitle_zh: Option<&str>,
    holder: StepName,
) -> Fixture {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    let subtitle: Vec<_> = subtitle_zh
        .map(|srt| ("subtitle_zh", "subtitle_zh.srt", srt))
        .into_iter()
        .collect();
    let files = |step| {
        if step == holder {
            subtitle.as_slice()
        } else {
            &[]
        }
    };
    let fetch = StepName::VideoFetch;
    fx.step(1, fetch, Some("Talk"), files(fetch), &[], &[]);
    let meta = [("language", MetaValue::Text(language.into()))];
    let transcribe = StepName::VideoTranscribe;
    fx.step(2, transcribe, None, files(transcribe), &[], &meta);
    let sections = sections.to_string();
    let files = [
        ("sections", "sections.json", sections.as_str()),
        ("transcript", "transcript.md", "old"),
    ];
    fx.step(
        3,
        StepName::VideoFrames,
        None,
        &files,
        &["frame-0.jpg"],
        &[],
    );
    fx
}

fn with_cache(fx: &Fixture) -> Vec<(OsString, OsString)> {
    let mut env = fx.env();
    env.push(("FLORI_CACHE_DIR".into(), cache_dir(fx).into()));
    env
}

fn cache_dir(fx: &Fixture) -> PathBuf {
    fx.job_dir().join("cache/translate")
}

async fn translate(fx: &Fixture) -> (PathBuf, StepResult) {
    let out = fx.out_dir("04-translate");
    let result = run_with(fx.job_dir(), &out, &with_cache(fx)).await;
    (out, result)
}

fn meta(out: &Path) -> BTreeMap<String, MetaValue> {
    pipeline::read_manifest(out).unwrap().meta
}

fn text(value: &str) -> MetaValue {
    MetaValue::Text(value.into())
}

#[tokio::test]
async fn arxiv_translation_keeps_protected_blocks_byte_for_byte() {
    let paper = fixture_text("paper.md");
    let fx = arxiv(&paper);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    // The fake echoes its input, so the restored translation equals the original.
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        paper
    );
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["translation"], "translation.md");
    assert_eq!(manifest.meta["language"], text("en"));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert_eq!(manifest.meta["translation_source"], text("ai"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0]["model"], "model-translate");
}

/// About 7000 characters, so every paragraph is a chunk of its own.
fn paragraph(n: usize, marker: &str) -> String {
    format!("Paragraph {n} {marker}. ")
        + &"The router assigns each token to two experts and adds their outputs. ".repeat(100)
}

fn long_paper(count: usize, marked: Option<(usize, &str)>) -> String {
    let paragraphs: Vec<String> = (1..=count)
        .map(|n| {
            paragraph(
                n,
                marked
                    .filter(|(m, _)| *m == n)
                    .map_or("", |(_, marker)| marker),
            )
        })
        .collect();
    paragraphs.join("\n\n") + "\n"
}

#[tokio::test]
async fn retry_after_chunk_three_fails_only_asks_for_three_to_n() {
    let fx = arxiv(&long_paper(5, Some((3, "FAKE_RATE_LIMIT"))));
    let (_, result) = translate(&fx).await;
    let failure = result.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.starts_with("chunk 3/5:"),
        "{}",
        failure.message
    );
    assert_eq!(fx.calls().len(), 3);
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 2);

    // The rate limit is over: the same chunk now gets a normal reply.
    let fixed = long_paper(5, None);
    let convert = pipeline::find_step(fx.job_dir(), StepName::ArxivConvert)
        .unwrap()
        .unwrap();
    fs::write(convert.join("original.md"), &fixed).unwrap();
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fx.calls().len(), 3 + 3);
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        fixed
    );

    // Everything is cached now.
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 6);
}

#[tokio::test]
async fn cache_key_includes_the_model() {
    let fx = arxiv(&long_paper(1, None));
    translate(&fx).await.1.unwrap();
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 1);
    let mut job = job_spec(Pipeline::Arxiv, AiTool::Claude);
    job.ai.model_translate = "other-model".into();
    fx.write_job(&job);
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 2);
}

#[tokio::test]
async fn exactly_25_chunks_are_still_translated() {
    let fx = arxiv(&long_paper(MAX_CHUNKS, None));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(meta(&out)["translated"], MetaValue::Bool(true));
    assert_eq!(fx.calls().len(), MAX_CHUNKS);
}

#[tokio::test]
async fn more_than_25_chunks_is_skipped_without_calls() {
    let fx = arxiv(&long_paper(MAX_CHUNKS + 1, None));
    let out = fx.out_dir("04-translate");
    // No FLORI_CACHE_DIR: the step must not get that far.
    run_with(fx.job_dir(), &out, &fx.env()).await.unwrap();
    let meta = meta(&out);
    assert_eq!(meta["skipped_reason"], text("too_long"));
    assert_eq!(meta["chunks"], MetaValue::Int(26));
    assert_eq!(meta["language"], text("en"));
    assert_eq!(meta["translated"], MetaValue::Bool(false));
    assert!(pipeline::read_manifest(&out).unwrap().files.is_empty());
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn chinese_and_unknown_sources_only_write_meta() {
    for (name, reason, language) in [
        ("lang-zh.md", "chinese", "zh"),
        ("lang-short.md", "unknown", "unknown"),
    ] {
        let fx = arxiv(&fixture_text(name));
        let (out, result) = translate(&fx).await;
        result.unwrap();
        let manifest = pipeline::read_manifest(&out).unwrap();
        assert!(manifest.files.is_empty());
        assert_eq!(manifest.meta["skipped_reason"], text(reason));
        assert_eq!(manifest.meta["language"], text(language));
        assert_eq!(manifest.meta["translated"], MetaValue::Bool(false));
        // Only a Chinese video gets punctuation; a Chinese paper is left alone.
        assert!(!manifest.meta.contains_key("punctuated"));
        assert!(!out.join("translation.md").exists());
        assert!(fx.calls().is_empty());
    }
}

/// What the fake CLI makes of a segment under FAKE_PUNCTUATE.
fn fake_punctuated(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join("，") + "。"
}

#[tokio::test]
async fn chinese_video_is_punctuated_in_video_frames_layout() {
    let spoken = fixture_text("lang-spoken-mixed.md");
    let lines: Vec<&str> = spoken.lines().collect();
    let edited = format!("{} FAKE_EDIT", lines[1]);
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": ["frame-0.jpg"], "text": "大家好 我是 Pacon FAKE_PUNCTUATE"},
        {"start_ms": 30000, "frames": ["frame-30000.jpg", "frame-41000.jpg"], "text": ""},
        {"start_ms": 60000, "frames": [], "text": lines[0]},
        {"start_ms": 90000, "frames": ["frame-90000.jpg"], "text": edited},
        {"start_ms": 120000, "frames": [], "text": lines[2]},
        {"start_ms": 150000, "frames": [], "text": lines[3]},
        {"start_ms": 3_690_000, "frames": [], "text": lines[4]},
        {"start_ms": 3_720_000, "frames": [], "text": "等的就是这部分复仇出清 太依赖菜单程序"},
        {"start_ms": 3_750_000, "frames": [], "text": "这一节 FAKE_REWRITE"},
    ]);
    let fx = video(&sections, "zh");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let punctuated = |line: &str| escape_markdown(&fake_punctuated(line));
    // The edited word goes back and so does the fix that does not sound the same, the fix that
    // does stays, and the section whose reply is other words keeps its transcribed text.
    let want = format!(
        "## 00:00\n\n![](assets/frame-0.jpg)\n\n大家好，我是，Pacon，FAKE\\_PUNCTUATE。\n\n\
         ## 00:30\n\n![](assets/frame-30000.jpg)\n\n![](assets/frame-41000.jpg)\n\n\
         ## 01:00\n\n{}\n\n## 01:30\n\n![](assets/frame-90000.jpg)\n\n{}\n\n\
         ## 02:00\n\n{}\n\n## 02:30\n\n{}\n\n## 1:01:30\n\n{}\n\n\
         ## 1:02:00\n\n等的就是这部分浮筹出清，太依赖菜单程序。\n\n\
         ## 1:02:30\n\n这一节 FAKE\\_REWRITE\n",
        punctuated(lines[0]),
        punctuated(&edited),
        punctuated(lines[2]),
        punctuated(lines[3]),
        punctuated(lines[4]),
    );
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    let sections: Vec<Section> = serde_json::from_value(sections).unwrap();
    let mut rendered = sections.clone();
    for section in rendered.iter_mut().filter(|s| s.has_text()) {
        section.text = match section.text.as_str() {
            "等的就是这部分复仇出清 太依赖菜单程序" => {
                "等的就是这部分浮筹出清，太依赖菜单程序。".to_owned()
            }
            "这一节 FAKE_REWRITE" => section.text.clone(),
            text => fake_punctuated(text),
        };
    }
    assert_eq!(want, crate::video::render_transcript(&rendered));
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert!(manifest.assets.is_empty());
    assert_eq!(
        manifest.meta,
        BTreeMap::from([
            ("language".to_owned(), text("zh")),
            ("translated".to_owned(), MetaValue::Bool(false)),
            ("punctuated".to_owned(), MetaValue::Bool(true)),
            ("sections".to_owned(), MetaValue::Int(8)),
            ("kept_original".to_owned(), MetaValue::Int(1)),
            ("corrected".to_owned(), MetaValue::Int(1)),
            ("reverted".to_owned(), MetaValue::Int(2)),
        ])
    );
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0]["model"], "model-translate");
    let prompt = calls[0]["prompt"].as_str().unwrap();
    assert!(prompt.starts_with(builtin(PromptName::Punctuate)));
    let (_, input) = prompt.rsplit_once(INPUT_MARKER).unwrap();
    let payload: Payload = serde_json::from_str(input.trim()).unwrap();
    let sent: Vec<&str> = payload.segments.iter().map(|s| s.text.as_str()).collect();
    let with_text: Vec<&str> = sections
        .iter()
        .filter(|s| s.has_text())
        .map(|s| s.text.as_str())
        .collect();
    assert_eq!(sent, with_text);

    // The checked reply is cached: a retry makes no call and writes the same transcript.
    let (again, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fx.calls().len(), 1);
    assert_eq!(
        fs::read_to_string(again.join("transcript.md")).unwrap(),
        want
    );
}

/// Text of section `i` in [`long_chinese`]: about 4000 characters, and no two alike, so no chunk
/// is answered from another's cache entry.
fn long_section(i: u64) -> String {
    format!("第{i}节") + &"股市大跌".repeat(1000)
}

/// `count` Chinese sections of about 4000 characters each.
fn long_chinese(count: u64) -> serde_json::Value {
    (0..count)
        .map(|i| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": long_section(i)}))
        .collect()
}

#[tokio::test]
async fn punctuation_chunks_are_smaller_than_translation_chunks() {
    let fx = video(&long_chinese(2), "zh");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(meta(&out)["punctuated"], MetaValue::Bool(true));
    // Two sections fit one translation chunk, but a punctuation chunk holds only one.
    assert_eq!(fx.calls().len(), 2);
    let segments: Vec<Segment> = (0..2)
        .map(|i| protect(&long_section(i), Pipeline::Video))
        .collect();
    assert_eq!(chunk(&segments, CHUNK_CHARS).len(), 1);
}

#[tokio::test]
async fn cache_key_includes_the_prompt() {
    let fx = video(&long_chinese(1), "zh");
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 1);
    // The same payload under another prompt, such as an edited one, is asked anew.
    let job = pipeline::read_job(fx.job_dir()).unwrap();
    let part = [protect(&long_section(0), Pipeline::Video)];
    let (env, cache) = (with_cache(&fx), cache_dir(&fx));
    let prompt = builtin(PromptName::Translate);
    ask_chunk(&env, &job, prompt, &cache, &part, "chunk 1/1")
        .await
        .unwrap();
    assert_eq!(fx.calls().len(), 2);
    // What translate sent first: the built-in text is the composed prompt, byte for byte.
    let prompt = builtin(PromptName::Punctuate);
    ask_chunk(&env, &job, prompt, &cache, &part, "chunk 1/1")
        .await
        .unwrap();
    assert_eq!(fx.calls().len(), 2);
}

/// translate with FLORI_PROMPTS_DIR as the executor writes it for `stored`.
async fn translate_with(fx: &Fixture, stored: &prompts::Stored) -> StepResult {
    let dir = fx.job_dir().join("prompts");
    prompts::write_dir(&dir, "计算机系统结构", stored).unwrap();
    let mut env = with_cache(fx);
    env.push(("FLORI_PROMPTS_DIR".into(), dir.into()));
    run_with(fx.job_dir(), &fx.out_dir("04-translate"), &env).await
}

/// What the calls from the `from`th on sent before the last input marker, each text once.
fn instructions_since(fx: &Fixture, from: usize) -> BTreeSet<String> {
    fx.calls()[from..]
        .iter()
        .map(|call| {
            let prompt = call["prompt"].as_str().unwrap();
            prompt.rsplit_once(INPUT_MARKER).unwrap().0.to_owned()
        })
        .collect()
}

#[tokio::test]
async fn the_domain_requirement_ends_the_instructions_and_edits_miss_the_cache() {
    let section = "## 领域要求(计算机系统结构)\n\n术语保留英文。\n";
    let cases = [
        (PromptName::Translate, PromptName::Punctuate),
        (PromptName::Punctuate, PromptName::Translate),
    ];
    for (name, other) in cases {
        let fx = match name {
            PromptName::Punctuate => video(&long_chinese(1), "zh"),
            _ => arxiv(&fixture_text("paper.md")),
        };
        // The other prompt's requirement and override play no part.
        let mut stored = prompts::Stored::default();
        stored.domain.insert(other, "不该出现".into());
        stored.overrides.insert(other, "也不该出现".into());
        translate_with(&fx, &stored).await.unwrap();
        let chunks = fx.calls().len();
        assert!(chunks > 0, "{name}");
        let sent = |text: String| BTreeSet::from([format!("{text}\n")]);
        assert_eq!(instructions_since(&fx, 0), sent(builtin(name).into()));

        // A requirement goes after the instructions and before the input; the cache misses.
        stored.domain.insert(name, "\n术语保留英文。 \n".into());
        translate_with(&fx, &stored).await.unwrap();
        assert_eq!(fx.calls().len(), 2 * chunks, "{name}");
        let want = format!("{}\n{section}", builtin(name));
        assert_eq!(instructions_since(&fx, chunks), sent(want));

        // So does an edited prompt.
        stored.overrides.insert(name, "改过的提示词".into());
        translate_with(&fx, &stored).await.unwrap();
        assert_eq!(fx.calls().len(), 3 * chunks, "{name}");
        let want = format!("改过的提示词\n\n{section}");
        assert_eq!(instructions_since(&fx, 2 * chunks), sent(want));

        // The same texts again are answered from the cache.
        translate_with(&fx, &stored).await.unwrap();
        assert_eq!(fx.calls().len(), 3 * chunks, "{name}");
    }
}

#[tokio::test]
async fn a_chinese_video_past_the_chunk_limit_is_skipped_without_calls() {
    // A Chinese subtitle does not lift the limit: it is no translation of a Chinese transcript.
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n股市\n";
    for subtitle in [None, Some(srt)] {
        let sections = long_chinese(MAX_CHUNKS as u64 + 1);
        let fx = video_with_subtitle(&sections, "zh", subtitle);
        let out = fx.out_dir("04-translate");
        // No FLORI_CACHE_DIR: the step must not get that far.
        run_with(fx.job_dir(), &out, &fx.env()).await.unwrap();
        let manifest = pipeline::read_manifest(&out).unwrap();
        assert_eq!(manifest.meta["skipped_reason"], text("too_long"));
        assert_eq!(manifest.meta["chunks"], MetaValue::Int(26));
        assert!(!manifest.meta.contains_key("punctuated"));
        assert!(manifest.files.is_empty());
        assert!(fx.calls().is_empty());
    }
}

#[tokio::test]
async fn invalid_reply_is_asked_again_once() {
    let fx = arxiv(&long_paper(1, Some((1, "FAKE_INVALID_ONCE"))));
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 2);

    let fx = arxiv(&long_paper(1, Some((1, "FAKE_INVALID"))));
    let failure = translate(&fx).await.1.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.contains("validation twice"),
        "{}",
        failure.message
    );
    assert_eq!(fx.calls().len(), 2);
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 0);
}

/// About 280 letters of English, enough for detection.
const FILLER: &str = "The router assigns each token to two experts and adds their outputs. \
    The router assigns each token to two experts and adds their outputs. \
    The router assigns each token to two experts and adds their outputs. \
    The router assigns each token to two experts and adds their outputs.";

#[tokio::test]
async fn a_segment_failing_twice_keeps_its_original_and_the_rest_is_translated() {
    let paper = format!(
        "# Method\n\nWe set `alpha` to $0.5$ FAKE_UNPLACED. {FILLER}\n\nThen `beta` follows. {FILLER}\n"
    );
    let fx = arxiv(&paper);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    // The fake drops the placeholders of the marked block twice and appends " (zh)" to the rest.
    let want = format!(
        "# Method (zh)\n\nWe set `alpha` to $0.5$ FAKE_UNPLACED. {FILLER}\n\n\
         Then `beta` follows. {FILLER} (zh)\n"
    );
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        want
    );
    let meta = meta(&out);
    assert_eq!(meta["translated"], MetaValue::Bool(true));
    assert_eq!(meta["untranslated_blocks"], MetaValue::Int(1));
    let calls = fx.calls();
    assert_eq!(calls.len(), 2);
    let reask = calls[1]["prompt"].as_str().unwrap();
    assert!(
        reask.contains(&format!(
            "{FEEDBACK_MARKER}\n上一次输出没有通过校验:id 2 缺少占位符 ⟦0⟧ ⟦1⟧。"
        )),
        "{reask}"
    );
    // Only a reply valid in every segment is cached: a retry asks again.
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 0);

    // The job.log line of the kept block names the chunk, the id and the reason.
    let job = pipeline::read_job(fx.job_dir()).unwrap();
    let segments = arxiv_segments(&paper);
    let chunks = chunk(&segments, CHUNK_CHARS);
    let prompt = builtin(PromptName::Translate);
    let replies = ask_all(&with_cache(&fx), &job, prompt, &segments, &chunks)
        .await
        .unwrap();
    assert_eq!(
        replies[1],
        Err("chunk 1/1: id 2 缺少占位符 ⟦0⟧ ⟦1⟧".to_owned())
    );
    assert_eq!(
        replies[2],
        Ok(format!("Then `beta` follows. {FILLER} (zh)"))
    );
}

#[tokio::test]
async fn a_segment_the_re_ask_breaks_keeps_its_first_translation() {
    // The re-ask fixes the first block and breaks the second, which the first reply had right.
    let paper = format!(
        "We set `alpha` FAKE_UNPLACED_FIRST. {FILLER}\n\nThen `beta` FAKE_UNPLACED_AGAIN. {FILLER}\n"
    );
    let fx = arxiv(&paper);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let want = format!(
        "We set `alpha` FAKE_UNPLACED_FIRST. {FILLER} (zh)\n\n\
         Then `beta` FAKE_UNPLACED_AGAIN. {FILLER} (zh)\n"
    );
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        want
    );
    assert_eq!(meta(&out)["untranslated_blocks"], MetaValue::Int(0));
    assert_eq!(fx.calls().len(), 2);
    // Every segment ended up valid, so a retry needs no call.
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 1);
    let (_, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fx.calls().len(), 2);
}

#[tokio::test]
async fn a_re_ask_unusable_as_a_whole_falls_back_to_the_first_reply() {
    let paper = format!(
        "We set `alpha` FAKE_UNPLACED_FIRST. {FILLER}\n\nThen `beta` FAKE_REASK_INVALID. {FILLER}\n"
    );
    let fx = arxiv(&paper);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let want = format!(
        "We set `alpha` FAKE_UNPLACED_FIRST. {FILLER}\n\n\
         Then `beta` FAKE_REASK_INVALID. {FILLER} (zh)\n"
    );
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        want
    );
    assert_eq!(meta(&out)["untranslated_blocks"], MetaValue::Int(1));
    assert_eq!(fx.calls().len(), 2);
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 0);
}

#[tokio::test]
async fn a_video_section_failing_twice_shows_its_original_alone() {
    // Speech has no placeholders but for a literal ⟦, which the marked section carries.
    let marked = format!("Bracket ⟦ here FAKE_UNPLACED. {FILLER}");
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": [], "text": marked},
        {"start_ms": 30000, "frames": [], "text": FILLER},
    ]);
    let fx = video(&sections, "en");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let want = format!(
        "## 00:00\n\n{}\n\n## 00:30\n\n{FILLER} (zh)\n\n> [!quote]- 原文\n> {FILLER}\n",
        escape_markdown(&marked)
    );
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    assert_eq!(meta(&out)["untranslated_blocks"], MetaValue::Int(1));
    assert_eq!(fx.calls().len(), 2);
}

#[tokio::test]
async fn a_chinese_section_failing_twice_is_kept_as_transcribed() {
    let marked = format!("这里有括号 ⟦ FAKE_UNPLACED {}", long_section(0));
    let sections = serde_json::json!([{"start_ms": 0, "frames": [], "text": marked}]);
    let fx = video(&sections, "zh");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    assert_eq!(
        transcript,
        format!("## 00:00\n\n{}\n", escape_markdown(&marked))
    );
    let meta = meta(&out);
    assert_eq!(meta["kept_original"], MetaValue::Int(1));
    assert!(!meta.contains_key("untranslated_blocks"));
    assert_eq!(fx.calls().len(), 2);
}

#[tokio::test]
async fn each_call_of_a_re_ask_is_recorded_and_cached_chunks_make_none() {
    let fx = arxiv(&long_paper(1, Some((1, "FAKE_INVALID_ONCE"))));
    let ai = fx.job_dir().join("ai");
    let env = |attempt: &str| {
        let mut env = with_cache(&fx);
        env.push(("FLORI_AI_LOG_DIR".into(), ai.clone().into()));
        env.push(("FLORI_STEP".into(), "translate".into()));
        env.push(("FLORI_ATTEMPT".into(), attempt.into()));
        env
    };
    let names = || {
        let mut names: Vec<String> = fs::read_dir(&ai)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        names.sort();
        names
    };
    let out = fx.out_dir("04-translate");
    run_with(fx.job_dir(), &out, &env("1")).await.unwrap();
    let first = names();
    assert_eq!(
        first,
        [
            "translate-a1-1.call.json",
            "translate-a1-1.err.txt",
            "translate-a1-1.out.json",
            "translate-a1-1.prompt.md",
            "translate-a1-2.call.json",
            "translate-a1-2.err.txt",
            "translate-a1-2.out.json",
            "translate-a1-2.prompt.md",
        ]
    );
    // The rejected reply is kept as the CLI printed it, and the re-ask carries the feedback.
    let rejected = fs::read_to_string(ai.join("translate-a1-1.out.json")).unwrap();
    assert!(rejected.contains(r#""result": "not json""#), "{rejected}");
    let reask = fs::read_to_string(ai.join("translate-a1-2.prompt.md")).unwrap();
    assert!(reask.contains(FEEDBACK_MARKER), "{reask}");
    assert!(
        !fs::read_to_string(ai.join("translate-a1-1.prompt.md"))
            .unwrap()
            .contains(FEEDBACK_MARKER)
    );

    // A manual retry finds the chunk cached: no call, no record.
    let out = fx.out_dir("04-translate");
    run_with(fx.job_dir(), &out, &env("2")).await.unwrap();
    assert_eq!(fx.calls().len(), 2);
    assert_eq!(names(), first);
}

#[tokio::test]
async fn auth_failure_stops_at_once() {
    let fx = arxiv(&long_paper(2, Some((1, "FAKE_AUTH"))));
    let failure = translate(&fx).await.1.unwrap_err();
    assert_eq!(failure.exit, StepExit::Auth);
    assert_eq!(fx.calls().len(), 1);
}

#[tokio::test]
async fn video_transcript_is_bilingual_per_section() {
    let first = "So the key idea is that every token picks two experts out of eight, \
                 and the router learns which experts are good at which kinds of input.";
    let third = "That is why the active parameter count stays at thirteen billion\n\
                 even though the model stores forty seven billion parameters in total.";
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": ["frame-0.jpg"], "text": first},
        {"start_ms": 30000, "frames": ["frame-30000.jpg", "frame-41000.jpg"], "text": " "},
        {"start_ms": 3_690_000, "frames": [], "text": third},
    ]);
    let fx = video(&sections, "fr");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let want = format!(
        "## 00:00\n\n![](assets/frame-0.jpg)\n\n{first}\n\n> [!quote]- 原文\n> {first}\n\n\
         ## 00:30\n\n![](assets/frame-30000.jpg)\n\n![](assets/frame-41000.jpg)\n\n\
         ## 1:01:30\n\n{third}\n\n> [!quote]- 原文\n\
         > That is why the active parameter count stays at thirteen billion\n\
         > even though the model stores forty seven billion parameters in total.\n"
    );
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert!(manifest.assets.is_empty());
    // The video path keeps video-transcribe's language code instead of guessing one.
    assert_eq!(manifest.meta["language"], text("fr"));
    // Without a Chinese subtitle the AI translates.
    assert_eq!(manifest.meta["translation_source"], text("ai"));
    assert_eq!(fx.calls().len(), 1);
}

#[tokio::test]
async fn a_chinese_manual_subtitle_is_the_translation_without_any_ai_call() {
    // Short on purpose: too few letters for detection, which a subtitle translation ignores.
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": ["frame-0.jpg"], "text": "Imagine a *tiny* robot."},
        {"start_ms": 30000, "frames": [], "text": "It reads #include <vector>."},
        {"start_ms": 60000, "frames": [], "text": "Nobody *translated* #this <part>."},
        {"start_ms": 90000, "frames": [], "text": " "},
    ]);
    // Cues go to the 30s window of their start: 00:01 and 00:12 to the first section, 00:30 to
    // the second, none to the third.
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n想象一个\n\n\
               2\n00:00:12,000 --> 00:00:16,000\n*很小*的机器人。\n\n\
               3\n00:00:30,000 --> 00:00:34,000\n它读到 #include <vector>。\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    // A section without a cue keeps only its original, escaped like everything else.
    let want = "## 00:00\n\n![](assets/frame-0.jpg)\n\n想象一个\\*很小\\*的机器人。\n\n\
                > [!quote]- 原文\n> Imagine a \\*tiny\\* robot.\n\n\
                ## 00:30\n\n它读到 \\#include \\<vector\\>。\n\n\
                > [!quote]- 原文\n> It reads \\#include \\<vector\\>.\n\n\
                ## 01:00\n\nNobody \\*translated\\* \\#this \\<part\\>.\n\n\
                ## 01:30\n";
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert_eq!(manifest.meta["translation_source"], text("subtitle"));
    assert_eq!(manifest.meta["language"], text("en"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn the_human_translation_video_transcribe_kept_is_used_too() {
    // TED-Ed: yt-dlp gave no language, the audio was English, so video-transcribe kept the
    // Chinese track as the translation.
    let sections =
        serde_json::json!([{"start_ms": 0, "frames": [], "text": "Imagine a tiny robot."}]);
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n想象一个小机器人。\n";
    let fx = video_with_subtitle_from(&sections, "en", Some(srt), StepName::VideoTranscribe);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    assert!(transcript.contains("想象一个小机器人。"), "{transcript}");
    assert_eq!(meta(&out)["translation_source"], text("subtitle"));
    assert!(fx.calls().is_empty());
}

/// Time and Chinese of each section in transcript.md, in order; None for an original-only one.
fn chinese_by_section(out: &Path) -> Vec<(String, Option<String>)> {
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    transcript
        .split("## ")
        .skip(1)
        .map(|part| {
            let (time, body) = part.split_once("\n\n").unwrap_or((part.trim(), ""));
            let paragraphs: Vec<&str> = body
                .split("\n\n")
                .filter(|p| !p.trim().is_empty() && !p.starts_with("![]"))
                .collect();
            let bilingual = paragraphs.iter().any(|p| p.starts_with("> [!quote]"));
            let chinese = bilingual.then(|| paragraphs[0].to_owned());
            (time.trim().to_owned(), chinese)
        })
        .collect()
}

#[tokio::test]
async fn cues_outside_a_section_with_text_are_dropped() {
    // 00:05 is before the first section, 01:05 lands on a keyframe-only section, 03:05 is after
    // the last one: none has an original to pair with, and none may shift into a neighbour.
    let sections = serde_json::json!([
        {"start_ms": 30000, "frames": [], "text": "Welcome back."},
        {"start_ms": 60000, "frames": ["frame-60000.jpg"], "text": ""},
        {"start_ms": 120000, "frames": [], "text": "The demo is over."},
        {"start_ms": 150000, "frames": [], "text": "Thanks for watching."},
    ]);
    let srt = "1\n00:00:05,000 --> 00:00:08,000\n片头\n\n\
               2\n00:00:35,000 --> 00:00:38,000\n欢迎回来。\n\n\
               3\n00:01:05,000 --> 00:01:08,000\n画面说明\n\n\
               4\n00:01:35,000 --> 00:01:38,000\n演示进行中\n\n\
               5\n00:02:05,000 --> 00:02:08,000\n演示结束了。\n\n\
               6\n00:03:05,000 --> 00:03:08,000\n片尾\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let section = |time: &str, chinese: Option<&str>| (time.to_owned(), chinese.map(str::to_owned));
    assert_eq!(
        chinese_by_section(&out),
        [
            section("00:30", Some("欢迎回来。")),
            section("01:00", None),
            section("02:00", Some("演示结束了。")),
            section("02:30", None),
        ]
    );
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn a_cue_in_a_window_without_a_section_does_not_join_the_previous_one() {
    // 01:05 falls in 01:00-01:30, where there is no section, right after a section with text.
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": [], "text": "Hello."},
        {"start_ms": 30000, "frames": [], "text": "The demo starts."},
        {"start_ms": 90000, "frames": [], "text": "The demo is over."},
    ]);
    let srt = "1\n00:00:05,000 --> 00:00:08,000\n你好。\n\n\
               2\n00:00:35,000 --> 00:00:38,000\n演示开始。\n\n\
               3\n00:01:05,000 --> 00:01:08,000\n画面说明\n\n\
               4\n00:01:35,000 --> 00:01:38,000\n演示结束了。\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let section = |time: &str, chinese: &str| (time.to_owned(), Some(chinese.to_owned()));
    assert_eq!(
        chinese_by_section(&out),
        [
            section("00:00", "你好。"),
            section("00:30", "演示开始。"),
            section("01:30", "演示结束了。"),
        ]
    );
}

#[tokio::test]
async fn a_chinese_subtitle_is_no_translation_of_a_chinese_transcript() {
    // A Chinese transcript next to a Chinese subtitle (a misreported language) gets punctuation.
    let spoken = fixture_text("lang-spoken-mixed.md");
    let lines: Vec<serde_json::Value> = spoken
        .lines()
        .enumerate()
        .map(|(i, line)| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": line}))
        .collect();
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n好的\n";
    let fx = video_with_subtitle(&serde_json::Value::Array(lines), "zh", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.meta["punctuated"], MetaValue::Bool(true));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(false));
    assert!(!manifest.meta.contains_key("translation_source"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    // The fake echoes, so every section keeps its text and the subtitle is nowhere.
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    assert!(!transcript.contains("好的") && !transcript.contains("[!quote]"));
    assert!(transcript.contains(&escape_markdown(spoken.lines().next().unwrap())));
    assert_eq!(fx.calls().len(), 1);
}

#[tokio::test]
async fn a_chinese_subtitle_translates_even_past_the_chunk_limit() {
    // Each section is about 12k characters, one chunk each: the AI path would skip as too_long.
    let long = "The router assigns each token to two experts and adds their outputs. ".repeat(175);
    let sections: Vec<serde_json::Value> = (0..MAX_CHUNKS as u64 + 1)
        .map(|i| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": long}))
        .collect();
    let srt: String = (0..MAX_CHUNKS as u64 + 1)
        .map(|i| {
            format!(
                "{}\n{} --> {}\n第 {i} 节\n\n",
                i + 1,
                srt_time(i * 30_000),
                srt_time(i * 30_000 + 1000)
            )
        })
        .collect();
    let fx = video_with_subtitle(&serde_json::Value::Array(sections), "en", Some(&srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.meta["translation_source"], text("subtitle"));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert!(!manifest.meta.contains_key("chunks"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    assert!(fx.calls().is_empty());
    let segments: Vec<Segment> = (0..=MAX_CHUNKS)
        .map(|_| protect(&long, Pipeline::Video))
        .collect();
    assert_eq!(chunk(&segments, CHUNK_CHARS).len(), MAX_CHUNKS + 1);
}

fn srt_time(ms: u64) -> String {
    let s = ms / 1000;
    format!(
        "{:02}:{:02}:{:02},{:03}",
        s / 3600,
        s / 60 % 60,
        s % 60,
        ms % 1000
    )
}

#[tokio::test]
async fn video_text_is_escaped_and_money_is_translated_whole() {
    let speech = "ChatGPT Plus costs $20 a month and Pro costs $200 a month, so call `__init__` \
                  first, then #include <vector> and *x* without any surprises in the notes. \
                  The rest of this section only exists so that detection sees enough letters.";
    let quoted = "- a list marker\n+ another one at the start of a line, plus ~tilde~ text.";
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": [], "text": speech},
        {"start_ms": 30000, "frames": [], "text": quoted},
    ]);
    let fx = video(&sections, "en");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    // The fake echoes, so the "translation" is the whole sentence, dollars included.
    let speech_md = escape_markdown(speech);
    let quoted_md = escape_markdown(quoted);
    let quote_lines: Vec<String> = quoted_md.lines().map(|l| format!("> {l}")).collect();
    let want = format!(
        "## 00:00\n\n{speech_md}\n\n> [!quote]- 原文\n> {speech_md}\n\n\
         ## 00:30\n\n{quoted_md}\n\n> [!quote]- 原文\n{}\n",
        quote_lines.join("\n")
    );
    assert_eq!(transcript, want);
    assert!(speech_md.contains("\\$20 a month and Pro costs \\$200"));
    assert!(quoted_md.starts_with("\\- a list") && quoted_md.contains("\n\\+ another"));
}
