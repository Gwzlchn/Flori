use super::*;

/// The merged text when the reply was usable.
fn merged(original: &str, reply: &str) -> Merged {
    merge(original, reply).unwrap_or_else(|| panic!("{original} -> {reply}: unusable"))
}

/// The guarantee, checked on its own: without punctuation and whitespace, `text` is `original`
/// but for single ideographs replaced as [`may_replace`] allows. Returns how many were replaced.
fn assert_guarantee(original: &str, text: &str) -> usize {
    let (a, b) = (keys(&units(original).units), keys(&units(text).units));
    assert_eq!(a.len(), b.len(), "{original} -> {text}");
    let replaced = a.iter().zip(&b).filter(|(x, y)| x != y);
    replaced
        .inspect(|(x, y)| assert!(may_replace(**x, **y), "{x} -> {y} in {text}"))
        .count()
}

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
        // A decimal point that opens a number stays; an ellipsis before a number is punctuation.
        ("降了.5个点", "降了.5个点。"),
        ("降了 .5个点", "降了，.5个点。"),
        ("等等...5月份", "等等……5月份。"),
        // Only an ASCII mark can be part of a number: 、 between digits is punctuation.
        ("第1 2 3条", "第1、2、3条。"),
        ("第一第二", "第一（第二）【】《》"),
    ];
    for (original, reply) in cases {
        let want = Merged {
            text: reply.into(),
            corrected: 0,
            reverted: 0,
        };
        assert_eq!(merge(original, reply), Some(want), "{reply}");
    }
}

#[test]
fn every_allowed_mark_passes_and_whitespace_collapses() {
    for mark in PUNCTUATION {
        let reply = format!("{mark}股市{mark}{mark}大跌{mark}");
        assert_eq!(merged("股市大跌", &reply).text, reply, "{mark}");
    }
    // Line breaks and indents would be Markdown structure; they become one space.
    assert_eq!(
        merged("股市 大跌", "股市，\n\n    大跌。\n").text,
        "股市， 大跌。"
    );
}

#[test]
fn same_sounding_replacements_are_kept() {
    // The homophones of job 16's Bilibili AI subtitle, and two older examples.
    let cases = [
        (
            "福建老哥等的就是这部分复仇出清",
            "福建老哥等的就是这部分浮筹出清。",
        ),
        (
            "当天结束巨龙科技成交量增至360万股",
            "当天结束，聚隆科技成交量增至360万股。",
        ),
        ("盘面上无其他装甲", "盘面上无其他庄家。"),
        ("给自己留一个安全店", "给自己留一个安全垫。"),
        ("神股集团的股价", "沈鼓集团的股价"),
        ("他先是小丹持续推高股价", "他先是小单持续推高股价"),
        ("福建老哥做装一只股票", "福建老哥坐庄一只股票。"),
        ("一度扶赢3个亿", "一度浮盈3个亿。"),
        // Heteronyms: 的 reads de and di, 得 de and dei, 地 di and de.
        ("这块把控的还算不错", "这块把控得还算不错。"),
        ("毫不掩饰的开始砸钱", "毫不掩饰地开始砸钱，"),
        // A numeral may come in for an ideograph that sounds the same.
        ("吃掉卖武当上面的大单即可", "吃掉卖五档上面的大单即可，"),
        ("他便以不低于卖溢价申买", "他便以不低于卖一价申买。"),
    ];
    for (original, reply) in cases {
        let got = merged(original, reply);
        assert_eq!((got.text.as_str(), got.reverted), (reply, 0), "{original}");
        assert_eq!(got.corrected, 1, "{original}");
        assert!(assert_guarantee(original, &got.text) > 0);
    }
    // Two fixes and one put back in a section, with the punctuation around each in place.
    let got = merged(
        "福建老哥等的就是这部分复仇出清到了14.33分他先是小丹持续推高股价",
        "福建老哥等的就是这部分浮筹出清。到了14:33分，他先是小单持续推高股价，",
    );
    assert_eq!(
        got,
        Merged {
            text: "福建老哥等的就是这部分浮筹出清。到了14.33分，他先是小单持续推高股价，".into(),
            corrected: 2,
            reverted: 1,
        }
    );
}

#[test]
fn other_changes_are_put_back_and_punctuation_stays() {
    let cases = [
        // A different initial: cai is not chai.
        (
            "说明他还是太依赖自动化菜单程序了",
            "说明他还是太依赖自动化拆单程序了。",
            "说明他还是太依赖自动化菜单程序了。",
        ),
        // A changed digit and a changed number mark.
        (
            "2017年股灾修复期间",
            "2018年，股灾修复期间。",
            "2017年，股灾修复期间。",
        ),
        ("到了14.33分", "到了14:33分。", "到了14.33分。"),
        // The transcript's numerals and number words stay, even for one that sounds the same.
        ("涨了十一个点", "涨了十亿个点。", "涨了十一个点。"),
        ("穿了一件", "穿了衣件。", "穿了一件。"),
        // 半 stays and does not come in.
        ("涨了一半", "涨了一板。", "涨了一半。"),
        ("涨了一板", "涨了一半。", "涨了一板。"),
        // A negation stays and does not come in, and 买 and 卖 do not swap.
        ("公司今年不分红", "公司今年部分红。", "公司今年不分红。"),
        ("超出订单不量级", "超出订单簿量级，", "超出订单不量级，"),
        ("这个布太好", "这个不太好。", "这个布太好。"),
        ("他今天买了", "他今天卖了。", "他今天买了。"),
        ("他今天卖了", "他今天买了。", "他今天卖了。"),
        // An inserted word; the mark before it stays where it was.
        ("今天聊股市", "今天，我们聊股市。", "今天，聊股市。"),
        ("今天聊股市", "今天聊股市吧。", "今天聊股市。"),
        // A deleted word; the mark goes after the word put back.
        (
            "然后那个我们去吧",
            "然后，我们去吧。",
            "然后那个，我们去吧。",
        ),
        // A replacement of another length; a mark inside it goes to its end.
        (
            "这部分复仇出清到了",
            "这部分浮动筹码出清，到了",
            "这部分复仇出清，到了",
        ),
        (
            "这部分复仇出清到了",
            "这部分浮动，筹码出清，到了",
            "这部分复仇，出清，到了",
        ),
        // ASCII: case, a term, an English name for a Chinese one.
        ("ai模型很火", "AI模型很火。", "ai模型很火。"),
        (
            "大家好我是培肯",
            "大家好，我是PAKEN。",
            "大家好，我是培肯。",
        ),
        ("大家好我是培肯", "大家好，我是培根。", "大家好，我是培肯。"),
        // A mark before a replacement put back stays before it, one after goes after it.
        ("今天菜很好", "今天，拆。很好", "今天，菜。很好"),
        // A run is kept or put back whole: a fix next to a changed digit goes back with it.
        ("一度扶赢3个亿", "一度浮盈4个亿。", "一度扶赢3个亿。"),
        // A mark on both sides of an inserted word lands once. Of different marks the first
        // stays, later quotes and brackets join it so that pairs stay whole, and a pair that
        // encloses nothing any more goes.
        ("我们走吧", "我们，那个，走吧。", "我们，走吧。"),
        ("我们走吧", "我们走吧（笑）。", "我们走吧。"),
        ("我们走吧", "我们走吧，“好吧”", "我们走吧，"),
        (
            "股市大跌我们走吧",
            "股市大跌。真是可怕！我们走吧。",
            "股市大跌。我们走吧。",
        ),
        (
            "我们现在走吧",
            "我们，那个，就是说。现在走吧",
            "我们，现在走吧",
        ),
        ("他说好走吧", "他说“好，就这样”走吧", "他说“好，”走吧"),
        ("他说好走吧", "他说“好”，就这样（吧）走吧", "他说“好”，走吧"),
        ("他说好走吧", "他说“好（就这样”走吧", "他说“好（”走吧"),
        // Two words merged, or one split: the break between ASCII words is content.
        (
            "machine learning",
            "machinelearning。",
            "machine learning。",
        ),
        ("machinelearning", "machine learning.", "machinelearning."),
        // A mark inside a number put back is dropped, as it would split the number.
        ("到了14.33分", "到了14：33分。", "到了14.33分。"),
        ("1,000股", "1，000股。", "1,000股。"),
        ("learning", "learn, ing!", "learning!"),
    ];
    for (original, reply, want) in cases {
        let got = merged(original, reply);
        assert_eq!(got.text, want, "{original} -> {reply}");
        assert_eq!(got.corrected, 0, "{original} -> {reply}");
        assert!(got.reverted > 0, "{original} -> {reply}");
        assert_eq!(assert_guarantee(original, &got.text), 0);
    }
}

#[test]
fn whatever_else_changes_the_original_content_survives() {
    let cases = [
        ("浮盈3亿到亏损4亿", "浮盈3亿到亏4亿。"),
        ("浮盈3亿到亏损4亿", "浮盈4亿到亏损3亿。"),
        // A decimal point dropped, turned into punctuation, or merged into the number before it.
        ("浮盈1.5亿", "浮盈15亿。"),
        ("浮盈1.5亿", "浮盈1。5亿"),
        ("1,000股", "1000股"),
        ("10:30开盘", "10，30开盘"),
        ("降了.5个点", "降了5个点。"),
        ("降了.5个点", "降了，5个点。"),
        ("涨了6 .06%", "涨了6，06%。"),
        ("涨了6 .06%", "涨了6.06%。"),
        ("涨了6.06%", "涨了6 .06%。"),
        ("Node.js", "Node。js"),
        // Hyphen-minus and other symbols are content.
        ("GPT-4", "GPT4"),
        ("-5度", "5度"),
        ("涨了60%", "涨了60。"),
        ("涨了", "涨了*"),
        ("涨了", "## 涨了"),
        ("3个", "３个"),
        ("v2 3", "v2... .3"),
    ];
    for (original, reply) in cases {
        let got = merged(original, reply);
        assert_eq!(got.corrected, 0, "{original} -> {reply}");
        assert!(got.reverted > 0, "{original} -> {reply}");
        assert_eq!(assert_guarantee(original, &got.text), 0, "{reply}");
    }
    // The reply's ellipsis would end the decimal point put back after it: the whole section
    // keeps its text.
    assert_eq!(merge("a .5", "a..5"), None);
}

#[test]
fn a_reply_that_is_no_copy_of_its_section_is_unusable() {
    assert_eq!(merge("股市", ""), None);
    assert_eq!(merge("股市", "。"), None);
    assert_eq!(merge("这一节讲的是股市", "说点别的。"), None);
    // Half of the units back in order is enough, and the rest goes back.
    assert_eq!(merged("股市大跌", "股市上涨。").text, "股市大跌。");
    assert_eq!(merge("股市大跌", "股上涨。"), None);
    assert_eq!(merge("股市大跌", "股。"), None);
    // So is a reply at most half of which is other text, as when the AI pulled in the start of
    // the next section; one with more is no copy, however long, and no slower for it.
    assert_eq!(merged("股市大跌", "股市大跌，我们走吧").text, "股市大跌，");
    assert_eq!(merge("股市大跌", "股市大跌，我们走了吧"), None);
    let section = "今天我们来聊一下股市的情况".repeat(15);
    assert_eq!(
        merge(&section, &format!("{section}{}", "的，".repeat(3000))),
        None
    );
    // A section of nothing but punctuation takes the reply's.
    assert_eq!(merged("。。", "。").text, "。");
}

#[test]
fn readings_decide_strictly_without_tones() {
    for (a, b) in [
        ('复', '浮'),
        ('仇', '筹'),
        ('丹', '单'),
        ('巨', '聚'),
        ('龙', '隆'),
        ('装', '庄'),
        ('甲', '家'),
        ('店', '垫'),
        ('神', '沈'),
        ('股', '鼓'),
        // Any modern reading of a heteronym: 单 also reads shan, 行 hang, 还 huan.
        ('单', '善'),
        ('行', '航'),
        ('还', '环'),
        ('的', '得'),
        ('的', '地'),
    ] {
        assert!(same_sound(a, b) && same_sound(b, a), "{a} {b}");
    }
    for (a, b) in [
        // No fuzzy initials or finals.
        ('菜', '拆'),
        ('在', '债'),
        ('四', '是'),
        ('牛', '刘'),
        ('肯', '根'),
        ('盘', '庞'),
        ('林', '宁'),
        // Letters, digits and marks are no ideographs that sound alike.
        ('a', 'a'),
        ('3', '3'),
        ('。', '。'),
        // Historical readings do not count: 沈 once read chen, 才 zai, 大 tai, 有 wei, 个 gan,
        // 不 fu.
        ('沈', '陈'),
        ('在', '才'),
        ('大', '太'),
        ('有', '为'),
        ('个', '干'),
        ('不', '复'),
        // Characters outside the modern table: traditional ones, 〇, and private-use characters
        // of old GB fonts that other pinyin tables read.
        ('來', '来'),
        ('萬', '万'),
        ('〇', '零'),
        ('\u{E815}', '\u{E815}'),
    ] {
        assert!(!same_sound(a, b) && !same_sound(b, a), "{a} {b}");
    }
}

#[test]
fn numerals_may_come_in_and_fixed_words_never_swap() {
    for (from, to) in [
        // Ideographs that sound alike, and a numeral for a word that sounds like it.
        ('复', '浮'),
        ('武', '五'),
        ('溢', '一'),
        ('是', '十'),
        ('照', '兆'),
        ('量', '两'),
        ('碗', '万'),
    ] {
        assert!(may_replace(from, to), "{from} -> {to}");
    }
    for (from, to) in [
        // An original numeral never goes, for a word or another numeral.
        ('五', '武'),
        ('一', '衣'),
        ('一', '亿'),
        ('十', '是'),
        ('兆', '照'),
        // 半, negations and 买卖 neither go nor come.
        ('半', '板'),
        ('板', '半'),
        ('不', '簿'),
        ('布', '不'),
        ('不', '部'),
        ('部', '不'),
        ('没', '美'),
        ('美', '没'),
        ('未', '为'),
        ('为', '未'),
        ('买', '卖'),
        ('卖', '买'),
    ] {
        // They sound the same: only the lists keep them apart.
        assert!(same_sound(from, to), "{from} {to}");
        assert!(!may_replace(from, to), "{from} -> {to}");
    }
}

#[test]
fn listed_characters_are_in_the_reading_table() {
    // One outside it can never match, so listing it would do nothing.
    let listed = NUMERALS.chars().chain(FIXED.chars());
    let missing: String = listed.filter(|c| !READINGS.contains_key(c)).collect();
    assert_eq!(missing, "");
}

#[test]
fn readings_cover_the_examples_and_market_terms() {
    let text = "菜单拆单复仇浮筹小丹小单巨龙聚隆装甲庄家安全店垫神股沈鼓集团培肯根\
                股票基金债券期货涨跌停板盘口筹码仓位杠杆融资融券做空多头空头\
                均线量价换手率市盈率净资产估值分红配股增发解禁减持回购庄家吸筹洗盘拉升出货\
                利好利空牛市熊市散户量化主力游资北向资金成交额市值龙头";
    let missing: String = text.chars().filter(|c| !READINGS.contains_key(c)).collect();
    assert_eq!(missing, "");
}

#[test]
fn reading_table_parses_whole() {
    assert_eq!(READINGS.len(), 8105);
    for (c, readings) in READINGS.iter() {
        assert!(!readings.is_empty(), "{c}");
        for r in readings {
            let plain = |x: char| x.is_ascii_lowercase() || x == 'ü' || x == 'ê';
            assert!(!r.is_empty() && r.chars().all(plain), "{c} {r}");
        }
    }
    let toneless = |c: char| READINGS[&c].join(",");
    assert_eq!(toneless('的'), "de,di,di,di");
    assert_eq!(toneless('绿'), "lu,lü");
    assert_eq!(toneless('嗯'), "ng,ng,ng");
    assert_eq!(toneless('欸'), "ai,ê,ê,ê,ê");
    assert_eq!(toneless('呣'), "m,m");
}

#[test]
fn units_count_like_the_rules_say() {
    let bare = |text: &str| keys(&units(text).units).into_iter().collect::<String>();
    assert_eq!(
        bare("好, 那 Python  写 machine learning"),
        "好那Python写machine learning"
    );
    assert_eq!(bare("3.5 1,000 .5 ...5 6 .06"), "3.5 1,000 .5 5 6 .06");
    // The gaps around a break are empty; its text is the break.
    let Units { units, gaps } = units("a ,b。");
    let texts: Vec<&str> = units.iter().map(|u| u.text).collect();
    assert_eq!(
        (texts, gaps),
        (vec!["a", " ,", "b"], vec!["", "", "", "。"])
    );
}

/// A deterministic pseudo-random sequence over a small alphabet, so equal runs are common.
fn sequence(seed: &mut u64, len: usize) -> Vec<u8> {
    (0..len)
        .map(|_| {
            *seed ^= *seed << 13;
            *seed ^= *seed >> 7;
            *seed ^= *seed << 17;
            b"abcd"[(*seed % 4) as usize]
        })
        .collect()
}

fn lcs(a: &[u8], b: &[u8]) -> usize {
    let mut row = vec![0; b.len() + 1];
    for x in a {
        let mut diagonal = 0;
        for (j, y) in b.iter().enumerate() {
            let up = row[j + 1];
            row[j + 1] = if x == y { diagonal + 1 } else { up.max(row[j]) };
            diagonal = up;
        }
    }
    row[b.len()]
}

#[test]
fn diff_finds_a_shortest_script() {
    let mut seed = 0x2545_f491_4f6c_dd1d;
    for round in 0..500 {
        let a = sequence(&mut seed, round % 13);
        let b = sequence(&mut seed, round % 11);
        let hunks = diff::diff(&a, &b, a.len() + b.len()).unwrap();
        // Between and around the runs the sequences are equal, and the runs are not empty.
        let (mut i, mut j, mut equal) = (0, 0, 0);
        for hunk in hunks.iter().chain([&diff::Hunk {
            a: a.len()..a.len(),
            b: b.len()..b.len(),
        }]) {
            assert_eq!(hunk.a.start - i, hunk.b.start - j);
            assert_eq!(a[i..hunk.a.start], b[j..hunk.b.start]);
            equal += hunk.a.start - i;
            (i, j) = (hunk.a.end, hunk.b.end);
        }
        assert!(hunks.iter().all(|h| !h.a.is_empty() || !h.b.is_empty()));
        assert_eq!(equal, lcs(&a, &b), "{a:?} {b:?}");
        // One edit short of the shortest script is too few.
        let edits = a.len() + b.len() - 2 * equal;
        if edits > 0 {
            assert_eq!(diff::diff(&a, &b, edits - 1), None);
        }
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
fn sections_take_their_merged_replies_and_are_counted() {
    let sections = [
        section(0, &["frame-0.jpg"], "福建老哥 做装"),
        section(30_000, &["frame-30000.jpg"], " "),
        section(60_000, &[], "一度扶赢3个亿 菜单"),
        section(90_000, &[], "最终离场"),
        section(120_000, &[], "后来"),
    ];
    // Replies only exist for the sections with text, in order.
    let replies = vec![
        "福建老哥，坐庄".to_owned(),
        "一度浮盈3个亿，拆单。".to_owned(),
        "最终，离场。".to_owned(),
        "另一段话。".to_owned(),
    ];
    let (punctuated, tally) = apply(&sections, replies);
    assert_eq!(
        tally,
        Tally {
            kept: 1,
            corrected: 2,
            reverted: 1,
        }
    );
    assert_eq!(
        punctuated,
        [
            section(0, &["frame-0.jpg"], "福建老哥，坐庄"),
            section(30_000, &["frame-30000.jpg"], " "),
            section(60_000, &[], "一度浮盈3个亿，菜单。"),
            section(90_000, &[], "最终，离场。"),
            section(120_000, &[], "后来"),
        ]
    );
}
