use super::*;

#[test]
fn inserted_punctuation_passes() {
    let cases = [
        ("今天我们聊一下股市", "今天,我们聊一下股市。"),
        ("今天我们聊一下股市", "今天，我们聊一下“股市”……"),
        // Existing punctuation may be replaced, and spaces next to Chinese may come or go.
        ("好,那我们开始 用 Python 写", "好。那我们开始：用Python写！"),
        // Numbers and terms with their inner marks intact.
        (
            "浮盈1.5亿 GPT-4 和 Node.js don't 10:30",
            "浮盈1.5亿，GPT-4和Node.js，don't？10:30。",
        ),
        ("machine learning", "machine, learning."),
        // Only an ASCII mark can be part of a number: 、 between digits is punctuation.
        ("第1 2 3条", "第1、2、3条。"),
        ("第一第二", "第一（第二）【】《》"),
    ];
    for (original, reply) in cases {
        assert_eq!(verify(original, reply).as_deref(), Some(reply), "{reply}");
    }
}

#[test]
fn every_allowed_mark_passes_and_whitespace_collapses() {
    for mark in PUNCTUATION {
        let reply = format!("{mark}股市{mark}{mark}大跌{mark}");
        assert_eq!(verify("股市大跌", &reply), Some(reply.clone()), "{mark}");
    }
    // Line breaks and indents would be Markdown structure; they become one space.
    assert_eq!(
        verify("股市 大跌", "股市，\n\n    大跌。\n").as_deref(),
        Some("股市， 大跌。")
    );
}

#[test]
fn any_other_change_fails() {
    let cases = [
        // A changed character, even a homophone fix.
        ("福建老哥做装一只股票", "福建老哥坐庄一只股票。"),
        ("一度扶赢3个亿", "一度浮盈3个亿。"),
        // Traditional to simplified is a change too.
        ("一起來看看", "一起来看看。"),
        // A dropped character.
        ("浮盈3亿到亏损4亿", "浮盈3亿到亏4亿。"),
        // A swapped number.
        ("浮盈3亿到亏损4亿", "浮盈4亿到亏损3亿。"),
        ("2017年股灾", "2018年股灾。"),
        // A decimal point dropped or turned into punctuation.
        ("浮盈1.5亿", "浮盈15亿。"),
        ("浮盈1.5亿", "浮盈1。5亿"),
        ("1,000股", "1000股"),
        ("10:30开盘", "10，30开盘"),
        // An added word.
        ("今天聊股市", "今天我们聊股市。"),
        ("今天聊股市", "今天聊股市吧。"),
        // Two words merged or one split: the break between ASCII words is content.
        ("machine learning", "machinelearning"),
        ("learning", "learn, ing"),
        ("Node.js", "Node。js"),
        // Hyphen-minus and other symbols are content.
        ("GPT-4", "GPT4"),
        ("-5度", "5度"),
        ("涨了60%", "涨了60。"),
        ("涨了", "涨了*"),
        ("涨了", "## 涨了"),
        // Case and full-width forms.
        ("ai模型", "AI模型"),
        ("3个", "３个"),
        // An empty reply.
        ("股市", ""),
    ];
    for (original, reply) in cases {
        assert_eq!(verify(original, reply), None, "{original} -> {reply}");
    }
}

fn section(start_ms: u64, frames: &[&str], text: &str) -> Section {
    Section {
        start_ms,
        frames: frames.iter().map(|f| (*f).to_owned()).collect(),
        text: text.into(),
    }
}

#[test]
fn a_failed_section_keeps_its_text_and_is_counted() {
    let sections = [
        section(0, &["frame-0.jpg"], "福建老哥 做装"),
        section(30_000, &["frame-30000.jpg"], " "),
        section(60_000, &[], "一度扶赢3个亿"),
        section(90_000, &[], "最终离场"),
    ];
    // Replies only exist for the sections with text, in order.
    let replies = vec![
        "福建老哥，做装".to_owned(),
        "一度浮盈3个亿。".to_owned(),
        "最终，离场。".to_owned(),
    ];
    let (punctuated, kept) = apply(&sections, replies);
    assert_eq!(kept, 1);
    assert_eq!(
        punctuated,
        [
            section(0, &["frame-0.jpg"], "福建老哥，做装"),
            section(30_000, &["frame-30000.jpg"], " "),
            section(60_000, &[], "一度扶赢3个亿"),
            section(90_000, &[], "最终，离场。"),
        ]
    );
}
