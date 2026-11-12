# Flori 契约

本文件是步骤 CLI、vault 布局和 HTTP API 的唯一权威。代码里的类型在 `src/pipeline.rs`(契约 1、2)和 `src/api.rs`(契约 3),改契约时三处在同一提交里一起改。

## 契约 1:步骤 CLI

```text
调用   内置:<current_exe> step <name> <job_dir> <out_dir>
       Python:python3 -I $FLORI_STEPS_DIR/<script>.py <子命令> <job_dir> <out_dir>
job_dir 只读:job.json、steps/<nn>-<name>/{manifest.json, 文件, assets/}
out_dir steps/<nn>-<name>.tmp/,执行器建好的空目录;成功后原子 rename 为 steps/<nn>-<name>/;失败时整体删除,下次尝试重建
cache   只有 video-fetch 与 translate 拿到 FLORI_CACHE_DIR=jobs/<id>/cache/<name>/(可写,各用各的;不在步骤目录里,跨尝试与手动重试保留)
ai      note 与 translate 拿到 FLORI_AI_LOG_DIR=jobs/<id>/ai/(可写,跨尝试保留,只增不删),放每次 AI CLI 调用的记录,见"AI CLI 固定调用"
日志   执行器把子进程 stdout/stderr 逐行追加到 jobs/<id>/job.log,每段以 `== <nn>-<name> attempt <n> ==` 开头,只追加不截断;步骤目录里不放日志
env    env_clear 后只注入 PATH LANG=C.UTF-8 HOME TMPDIR FLORI_STEP FLORI_ATTEMPT,再加下表白名单
退出码 0 成功,必须有 manifest.json,缺了按 20 处理;10 可重试;20 永久失败;30 凭据失效(处理同 20,UI 标注);其它退出码或信号按可重试处理
```

- `<nn>` 是从 01 开始的两位序号,即 steps 表的 seq。
- 子进程放进独立进程组。超时或取消时先给进程组发 SIGTERM,10s 后 SIGKILL。
- 自动重试间隔 30s、120s;AI 步骤只尝试 1 次,不自动重试。达到尝试次数上限后,步骤和 job 都记为 failed。
- FLORI_ATTEMPT 与日志头里的 attempt 是该步骤的累计尝试次数,手动 retry 后继续累加。超时按可重试处理,steps.exit_code 记 null。
- 取消时执行器给步骤进程组发 SIGTERM。AI 步骤收到 SIGTERM 后立即 SIGKILL 自己的 CLI 进程组并退出 10;job 与步骤状态以 HTTP 层写入的 canceled 为准。
- 按步骤名找前序产物:`steps/*-<name>/manifest.json`(`pipeline::find_step`)。
- translate=off 时不排入 translate。auto 时总是排入;跳过时(见下文 translate 规则)照常建目录,manifest 不带 files,只在 meta 里写 skipped_reason。
- 正文图片一律放 `assets/<basename>`,Markdown 里写 `![](assets/<basename>)`。publish 按步骤顺序合并 manifest.files,同名时后面的步骤覆盖前面的,只允许 translate 覆盖 transcript;assets 重名按 20 处理;note 与 translate 不输出 assets,只引用 video-frames 或 arxiv-convert 的 assets。
- B站多 P 只取 P1。

### job.json

服务端建 job 时写一次,之后不改,不放密钥。

```json
{
  "job_id": 12,
  "pipeline": "video",
  "source": {"platform": "youtube", "id": "dQw4w9WgXcQ", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
  "domain": "ml",
  "collection": null,
  "translate": "auto",
  "ai": {"tool": "claude", "model_note": "claude-opus-5-5", "model_translate": "claude-sonnet-5-5", "cli_version": "2.1.319"},
  "created": "2026-11-08T10:00:00+08:00",
  "public_url": "http://nas.lan:8780",
  "vault_name": "flori"
}
```

### manifest.json

```json
{"title": "str 或 null", "files": {"逻辑名": "相对步骤目录的路径"}, "assets": ["basename"], "meta": {"键": "标量或字符串数组"}}
```

四个键都必须出现,不允许多余的键。

### 额外注入的 env

| 步骤 | env |
|---|---|
| video-fetch | FLORI_STEPS_DIR(运行 `whisper.py t2s`)、FLORI_CACHE_DIR |
| video-fetch(youtube) | HTTPS_PROXY、HTTP_PROXY = $FLORI_PROXY |
| video-fetch(bilibili) | FLORI_BILI_SESSDATA(服务端有值时才传);不带代理 |
| video-transcribe | FLORI_WHISPER_MODEL、FLORI_STEPS_DIR |
| publish | FLORI_VAULT_DIR=$DATA/vault |
| note、translate | HTTPS_PROXY、HTTP_PROXY = $FLORI_PROXY;CLAUDE_CODE_DISABLE_CLAUDE_MDS=1;DISABLE_AUTOUPDATER=1;DISABLE_UPDATES=1;CLAUDE_CODE_OAUTH_TOKEN(存在时);CODEX_HOME(tool=codex 时);FLORI_AI_LOG_DIR;translate 另加 FLORI_CACHE_DIR |
| feed.py(不是步骤,同样 env_clear) | FLORI_PROXY(只经 yt-dlp `--proxy` 使用,B站请求显式不走代理)、FLORI_BILI_SESSDATA |

### 步骤表

| 步骤 | 资源 | 输出 | 超时 | 尝试 |
|---|---|---|---|---|
| video-fetch | net | video.mp4(发布后保留在 job 目录)、可选 subtitle.srt(转写用的人工字幕,选轨规则见下)、subtitle_zh.srt(中文人工字幕,作译文)、manual_subtitle_<lang>.srt(原声未知时的人工字幕)与 ai_subtitle_<lang>.srt(B站 AI 字幕),后两种待 video-transcribe 核对语言;meta: title, uploader, duration_ms, published, width, height, fps, vcodec, acodec, tags(字符串数组), filesize_bytes(video.mp4 的字节数),有 subtitle.srt 时另有 subtitle_language;除 filesize_bytes 外取 yt-dlp 所报下载格式的值,逐项校验:宽高 1–65535 的整数,fps 在 (0, 1000],编码只含字母数字与 `._+-` 且不超过 64 字符,`none` 不写,标签去空白与重复,单个不超过 100 字符、合计不超过 500 字符;不合规、拿不到或标签为空时省略该项 | 30m | 3 |
| video-transcribe | whisper | transcript.json、可选 subtitle_zh.srt;人工字幕优先(subtitle,或与音频语言相同的 manual_subtitle_<lang>),其次与音频语言相同的 B站 AI 字幕,最后 whisper;meta: origin(subtitle\|whisper), language, language_probability,origin 为 subtitle 时另有 subtitle_kind(manual\|ai) | 2h | 2 |
| video-frames | cpu | assets/frame-<ms>.jpg、sections.json `[{start_ms, frames:[basename], text}]`(30s 一节,去重后每节至多 2 张关键帧,全片不设总数上限)、由它渲染的 transcript.md | 1h | 2 |
| arxiv-fetch | net | page.html、资源原件;meta: arxiv_id(去版本号), version("vN",拿不到时省略), title, authors, abstract, html_source(arxiv\|ar5iv), published(arXiv API 的第一版日期 YYYY-MM-DD)与 primary_category(如 cs.LG),后两项拿不到时省略;categories(API 列出的全部分类,字符串数组,去重,每项至多 64 字符、至多 32 项)、comment(作者备注)与 journal_ref(期刊引用)取自 API,压成一行、去掉控制字符,至多 1000 字符,没有时省略;官方页截断且没有 ar5iv 时退出 10,两处都确定没有 HTML 时退出 20 | 15m | 3 |
| arxiv-convert | cpu | original.md、figures.json、assets/;公式取 MathML alttext 写成 `$…$`/`$$…$$`,其中未转义的 `$`(`\text{}` 里的公式)依次换成 `\(`、`\)`,公式编号去掉 `$`,公式只在自己的定界符处结束;正文文字里的 `[`、`]`、`!` 一律转义,链接、图片与脚注只由 convert 写出,链接地址百分号编码;figures.json 见下 | 10m | 2 |
| note | ai | note.md(读原文与全部关键帧或 figures.json 里的全部论文图,只受单次请求的上限约束;中文输出,只有正文,可用 `![描述](assets/<basename>)` 插图);单次调用超时 1800s | 35m | 1 |
| translate(auto 时排入) | ai | arXiv:translation.md;视频:读 sections.json,非中文写双语 transcript.md,中文写加标点的 transcript.md;每块调用超时 600s | 3h | 1 |
| publish | local | meta: vault_path(相对 FLORI_VAULT_DIR,含 vault 名), note_file(主笔记文件名), description(同 frontmatter 的 description,抽不到时省略) | 5m | 2 |

全局 Semaphore(2) 限制同时运行的步骤数,whisper 与 ai 各另有 Semaphore(1),local 不占全局名额。

### manifest.files 逻辑名

后续步骤和 publish 只按逻辑名找文件,不猜文件名。

| 步骤 | files | assets |
|---|---|---|
| video-fetch | video、subtitle(有可用于转写的人工字幕时,选轨规则见下)、subtitle_zh(原声确定不是中文且有中文人工字幕时)、manual_subtitle_<lang>(原声未知时该语言最好的人工字幕)、ai_subtitle_<lang>(B站没有 subtitle 而有该语言的 AI 字幕时) | 无 |
| video-transcribe | transcript_json、subtitle_zh(音频判定不是中文且有 manual_subtitle_zh 时) | 无 |
| video-frames | transcript、sections | frame-<ms>.jpg |
| arxiv-fetch | page | 无;资源原件只作为普通文件留在步骤目录 |
| arxiv-convert | original、figures | 正文图片 |
| note | note | 无;正文引用 video-frames 或 arxiv-convert 的 assets |
| translate | arXiv:translation;视频:transcript(覆盖 video-frames 的) | 无 |
| publish | 无 | 无 |

- 进 vault 的逻辑名只有 note、transcript、original、translation:note 写成 `<标题>.md`,其余写成 `<逻辑名>.md`;合并后的 assets 放进 `assets/`。其余逻辑名只留在 job 目录。
- 标题取步骤顺序中第一个非 null 的 manifest.title。video-fetch 的 manifest.title 是转成简体的标题,meta.title 保留平台原标题。
- transcript.json:`{"language": str|null, "segments": [{"start_ms": int, "end_ms": int, "text": str}]}`。
- figures.json:`[{"label": str, "caption": str, "images": [basename]}]`,按页面顺序,每个最外层的 figure 元素一项(ltx_figure、ltx_table 等,不在跳过的元素里);它没有自己的图注时,里面每个带图注的 figure 也各算一项(LaTeXML 把同一浮动体里并排的几张图写成这样)。label 是图注的编号标签去掉末尾的冒号或句点,如 "Figure 2",没有时为空串;caption 是图注其余的纯文本,公式写成 `$TeX$`,脚注不算,压成一行、去掉控制字符,至多 1000 字符;images 是它里面的图片(含嵌套面板与 object,不含自成一项的 figure 里的)在 assets 里的名字,按页面顺序去重。没有图片的不算,所以纯表格不在其中。
- sections.json 的 text 是未转义的原文;写进 Markdown 的一方(video-frames 的 transcript.md、translate 的双语版与加标点版)统一用 `pipeline::escape_markdown` 转义。Section 类型、取前序文件(`pipeline::step_file`)与节时间格式(`pipeline::clock`)只在 src/pipeline.rs 定义一次。
- translate 的 meta 总有 language 与 translated(bool)。language 在视频路径沿用 video-transcribe 的 meta.language(为 unknown 时改用判定值);arXiv 取判定值:zh、ja、ko、en(拉丁字母为主;arXiv 论文几乎都是英文,与视频的 en 保持一致)、other(其它文字)或 unknown。跳过时另有 skipped_reason(chinese、unknown、too_long;chinese 只出现在 arXiv),too_long 时另有 chunks(int)。译出时另有 translation_source:ai(调用 AI)或 subtitle(视频用 video-transcribe 或 video-fetch 的 subtitle_zh)。中文视频加了标点时 translated 为 false,另有 punctuated(true)、sections(送去加标点的有文字的节数,int)、kept_original(回复用不上、保留原文的节数,int)、corrected(采用的同音替换处数,int)与 reverted(还原的其它改动处数,int)。

- 简体中文:video-fetch 把留下的每条中文字幕(subtitle.srt 为中文时、subtitle_zh.srt、manual_subtitle_zh.srt、ai_subtitle_zh.srt)与中文标题(有汉字、没有假名和谚文,中点 `・` 不算假名)用 `python3 -I $FLORI_STEPS_DIR/whisper.py t2s <in.json> <out.json>` 转成简体,输入输出都是字符串数组,与 whisper 输出所用的 OpenCC t2s 相同;没有中文时不调用。转写稿、人工译文、job 标题、主笔记文件名与 frontmatter 的 title 因此都是简体。
- video-fetch 的下载:
  - yt-dlp 把视频下到 FLORI_CACHE_DIR,输出模板 `video.%(format_id)s.%(ext)s`,靠它默认的 `.part` 续传;`--ignore-config`,不加 `--no-continue` 或 `--no-part`。超时、取消或失败时缓存原样保留,下次尝试与手动重试从断点接着下;已下完的视频不再下载。
  - 例外是被打断的 remux:`--remux-video` 让 ffmpeg 直接写 `<文件名>.mp4`,写完 yt-dlp 才删原文件 `<文件名>.<ext>`;SIGTERM 下 ffmpeg 会把截断的 mp4 收尾成能播放的短文件,下次 yt-dlp 会当它已下完。所以每次尝试开始先删掉旁边还有同名原文件的 mp4,由 yt-dlp 从原文件重新 remux。合并先写 `.temp.mp4` 再改名,不受影响。
  - 文件名带 format_id:合并下载的分片本来就带 `.f<format_id>`,单一格式却直接写 `<文件名>.part`,文件名不带 format_id 时站点换了格式也会接着上一个格式的残片下。
  - 整步成功时才把视频移成 out_dir 的 video.mp4,并清空缓存目录(含别的格式的残片);字幕仍直接写 out_dir,每次尝试重下。
  - 视频下完后 job.log 记一行大小、用时与平均速度(十进制单位),如 `video-fetch: 31.2 MB in 1384 s (22.5 KB/s)`。续传时速度只算本次下载的字节,写成 `video-fetch: 31.2 MB, 19.2 MB of it in 853 s (22.5 KB/s), the rest from earlier attempts`;此前的字节取尝试开始时缓存里同一格式的文件大小之和。
- video-fetch 的字幕:取人工字幕与 B站 AI 字幕,不取 YouTube 自动字幕;尽力而为,失败只记日志,由 whisper 转写。
  - 原声语言取 yt-dlp 的 language 的主子标签(`en-US` 记 en);不是纯小写字母标签时按未知处理。YouTube 的 language 来自音轨或原声自动字幕(yt-dlp 同时把后者列为 `<lang>-orig`),没有别的字段知道更多。yt-dlp 的 B站提取器不给 language,B站总按未知处理。
  - subtitle.srt:只在原声已知时写,只取原声语言的轨,原声为中文时没有中文轨再用英文。同一语言内简体先于繁体,不带地区的标签先于带地区的。
  - subtitle_zh.srt:只在原声确定不是中文时写,取最好的中文轨。
  - manual_subtitle_<lang>.srt:原声未知时,`--sub-langs` 里每种语言取最好的一条人工字幕,不写 subtitle.srt 与 subtitle_zh.srt:中文轨可能是原话,也可能是社区译文。
  - `--sub-langs`:原声是 zh、en 以外的已知语言时为 `<原声>.*,zh.*`(英文轨用不上,而任一语言下载失败会中止整次字幕下载);其余情况为 `zh.*,en.*`。B站另按同样的语言加 `ai-<lang>`,即 `zh.*,en.*,ai-zh,ai-en`。
  - B站 AI 字幕 `ai-<lang>`:只对登录的 SESSDATA 列出,没有 SESSDATA 时照旧只有人工字幕。<lang> 是原声时它是语音识别,否则是机器翻译,B站不标明是哪种。没有 subtitle.srt 时才保留为 ai_subtitle_<lang>.srt(与 manual_subtitle_<lang>.srt 并存),有 subtitle.srt 时删除;从不作 subtitle.srt 或 subtitle_zh.srt。
- video-transcribe 的来源:有 subtitle 时直接用,subtitle_kind=manual,language_probability 为 1。否则有 manual_subtitle_<lang> 或 ai_subtitle_<lang> 时先用 `whisper.py detect` 判定音频语言(与 whisper 转写时的判定相同,不解码文字):有该语言的人工字幕就原样用它(subtitle_kind=manual),否则有该语言的 AI 字幕就用它(subtitle_kind=ai),language 与 language_probability 取判定值;都没有则 whisper 转写。判定不是中文且有 manual_subtitle_zh 时,把它复制成本步的 subtitle_zh.srt,作人工译文。
  - whisper 判定为 zh 时,每个窗口都带一句简体、带标点的中文提示(hotwords),输出再经 OpenCC t2s 转成简体;其它语言不带提示,不转换。
  - 带提示时 whisper 常把整个 30s 窗口出成一段,而 video-frames 按段起点分节,所以 zh 的段在后面紧跟汉字的 `。！？；，,!?;` 处切开,各片按字数比例分时间。切点后是汉字,`push_text` 拼回时不加空格,文字不变。
- arxiv-fetch:官方 HTML 偶尔返回 200 但内容被截断,必须检查完整闭合;ar5iv 对没有 HTML 的论文返回 200 落地页,必须识别;两者都不可用时退出 20,不回退 PDF。
- translate:
  - 判定:去掉公式、代码、URL、时间戳后,汉字/(汉字+字母) ≥ 0.3 且几乎没有假名和谚文 → 中文:arXiv 跳过,视频加标点;可比较的字母不足 200 个 → unknown,跳过。判定函数只在 Rust 里实现一次。
  - 跳过:判为中文的 arXiv 总是跳过;判为 unknown 或超过 25 块时跳过,但非中文视频有 subtitle_zh 时不跳过,因为不调用 AI。
  - arXiv:按空行切块,每块约 12k 字符;`$$…$$`、代码、表格、图片行、References 原样保留(文末的脚注定义 `[^n]:` 不算 References,照常翻译);行内公式、代码、URL、引用号(`[12]`,或 arxiv-convert 转义后的 `\[12\]`)与脚注标记 `[^n]` 换成占位符,译完还原;原文里字面的 `⟦` 也作为占位符保护;脚注定义行首的 `[^label]: ` 不送翻译,原样保留;列表项首块不是段落时 arxiv-convert 把标记单独写一行(如 `10.`、`4. -`),下一行缩进到该项内容列时,标记行连同下一行的缩进不送翻译,原样写在该块前且不隔空行;只有列表标记的块原样保留;表格和只含图片的行在最后一个同类行结束,列表里紧跟的下一项不并入;代码围栏按 CommonMark 识别(开围栏的缩进从列表项内容列算起,不设绝对上限;关闭行最多比开围栏深 3 格,同字符、长度不短于开围栏、只含围栏字符);不校验数字。
  - 非中文视频:按 sections.json 的节翻译,每节写成"关键帧 + 中文 + 折叠的原文"(`> [!quote]- 原文`);没有中文的节只写原文,不折叠。译文与原文每行用 `pipeline::escape_markdown` 转义。
    - 有 subtitle_zh(video-transcribe 的优先,其次 video-fetch 的;两者不会同时存在)时不调用 AI:每条中文 cue 按起点归入 `start_ms / SECTION_MS * SECTION_MS` 那个 30s 窗口的节(`pipeline::SECTION_MS`,与 video-frames 分节相同)。窗口没有节、或该节没有原文时丢弃这条 cue,第一节之前的 cue 同样丢弃;没有 cue 的节只写原文。
    - 没有 subtitle_zh 时由 AI 逐块翻译,转写不做占位符(只保护字面 `⟦`)。
  - 中文视频:不翻译,也不用 subtitle_zh;按节由 AI 加标点并改正同音错字(`prompts/punctuate.md`),模型、CLI、分节协议、缓存、重问与失败分类都与翻译相同。转写原本带标点时同样处理,字幕和 whisper 的标点都不可靠。
    - 回复要把每个字写回来,所以每块约 6k 字符,是翻译的一半。
    - 内容单位:去掉标点和空白后,每个字符是一个单位。标点只有 ASCII 的 `, . ; : ? ! " ' ( ) [ ] { }` 与 `，。、；：？！．“”‘’＂＇「」『』（）［］｛｝《》〈〉【】〔〕…⋯—–―·・`;连字符 `-`、`~`、`/`、`%` 等符号都是内容。夹在两个 ASCII 字母或数字之间的 ASCII 标点是数字或词的一部分(3.5、1,000、10:30、Node.js),算内容;数字开头的小数点(.5,不接在另一个 `.` 之后)也算内容;两个 ASCII 字母或数字之间、字母或数字与这种小数点之间的断开(空白或标点)也是一个单位,单词与数字不能合并或拆开。
    - 逐节对齐:原文与回复的单位序列取最短编辑脚本(Myers)。回复按序带回的原文单位不足原文的一半、或不足回复自身的一半(回复大半是别的内容,如 AI 挪进了别节的文字)时,该节保留原文、计入 kept_original,不重问;这也让对齐所用的内存只随原文长度增长,与回复多长无关。否则结果以原文的单位为准:相同处照抄;回复替换的一段只在两边等长、每对都是汉字、且每对有一个共同的不带声调的拼音时整段采用,计 1 处 corrected;其它改动(增、删、长度不同的替换、数字、ASCII、读音不同)整段还原为原文,计 1 处 reverted。
    - 读音:《通用规范汉字字典》的现代读音(pinyin-data 的 kTGHZ2013,8105 字,随代码附带),多音字任一现代读音都算,古音和旧读不算("在"不会变成旧读 zāi 的"才");表外的字(繁体字和"〇"等)不替换,也不换进来。声母韵母必须完全相同,不做 c/ch、z/zh、s/sh、n/l 或前后鼻音之类的模糊。下列汉字一换意思就变,读音相同也受限。汉字数字与数量词(零一二两三四五六七八九十百千万亿、壹贰叁肆伍陆柒捌玖拾佰仟、兆廿卅仨俩幺)可以换进来,"卖武当"可以改成"卖五档";原文里的不替换,"十一"不会变成"十亿"。换进来的数字两侧不能紧挨别的数字(汉字数字、阿拉伯数字等),否则那个数的大小会变:"二是"不会变成"二十","十义"不会变成"十亿","就是"也不会整个换成"九十"。"半"、否定词(不没无非未别莫勿否毋甭)与买卖既不替换也不换进来:"一半"与"一板"不互换,"不分红"不会变成"部分红",买卖两字只差声调。
    - 标点全部取自回复,回复新加的单词断开是还原的改动,其中的标点随之去掉。其余标点:两侧单位在结果里仍相邻的,原位保留;挨着还原段的放到该段末尾,但紧跟在保留单位之后、后面是还原段的留在该段开头;落点会改变数字或词的切分时丢弃;几段标点挪到同一处时,中间已无内容的一对引号或括号去掉,该处有了标点之后,后面几段只补上其中的引号和括号,免得插入的一句话两侧的标点留下"（）"或叠成"。！"。结果去掉标点和空白后与原文相同,只在 corrected 处换成同音字;写出前逐节校验这一点,不成立时该节同样保留原文、计入 kept_original。
    - 结果把空白压成一个空格。transcript.md 与 video-frames 的版式相同(都用 `video::render_transcript`):每节时间标题、关键帧、用 `pipeline::escape_markdown` 转义的文字,没有原文折叠。
  - 串行;每块结果按 sha256(prompt‖model‖payload) 缓存,手动重试只补未缓存的块;校验失败时带反馈再问 1 次,仍失败退出 10。
  - 超过 25 块不调用 AI:只写 meta.skipped_reason=too_long 与块数,退出 0;笔记与原文照常发布,详情页显示原因。加标点按它自己的块大小计;用 subtitle_zh 翻译的视频不受此限。
- note:
  - 一次调用同时发原文与图,图按序号 [N] 列出,序号即附图顺序。视频按时间顺序发 sections 里的全部关键帧,每张写时间与该节文字前 150 字;笔记每个要点只要有能说明它的画面就配一张。论文按 figures.json 的顺序发每个图的全部图片,每张都写该图的 "label: caption" 前 300 字;只取 arxiv-convert 的 manifest.assets 里有的名字,同一张图只发一次。没有 figures.json(早于它转换的论文)时不发图。
  - 图数和总量不设固定上限,只受单次请求的两条上限约束:至多 600 张,超出时视频按时间、论文按位置均匀取;缩图之后所发图片的 base64 合计至多 20 MiB(API 单次请求体上限 32 MB,含正文,留出余量),超出时按序号位置均匀丢图,首尾保留,job.log 记一行。
  - 只发内容确实是 jpeg、png、gif、webp 的图,media_type 按文件头判断,不看扩展名;扩展名是图片但内容不是的文件跳过。
  - SVG(去掉 BOM、XML 声明等处理指令、注释与 doctype 后根元素是 `<svg`;doctype 带内部子集 `[` 的不算)由 ffmpeg 的 librsvg 解码器画成 PNG 只送模型:SVG 经 stdin 送入(`-f svg_pipe -frame_size <字节数> -i pipe:0`,`-protocol_whitelist pipe`),没有文件名,相对引用与外部引用都解析不到;先画成长边上限那么宽,像素超过上限平方的两倍(`-max_pixels`)时改画成那么高,声明的尺寸只决定宽高比,两种都画不出的跳过;画好后按预乘 alpha 铺白底,再按下条的上限检查。超时与环境变量同下条的缩图;失败只在 job.log 记一行,ffmpeg 的报错并入这一行。
  - 送模型的图有长边上限:关键帧 768px,与源视频分辨率无关;论文图 1568px。大于 1 MiB 或长边超限的图用 ffmpeg 缩到上限以内只送模型(PNG 仍为 PNG,其余转 JPEG),笔记里仍引用原图;WebP 读不到尺寸,只按字节判断。缩完仍超限或缩不了的图不发。ffmpeg 按文件头固定 demuxer、只允许 file 协议,环境变量只给 PATH、LANG、TMPDIR。
  - 模型用 `![描述](img:N)` 插图,落盘时换成 `![描述](assets/<basename>)`;N 不在清单里时整条图片标记删除。`assets/` 下的图片原样保留,其它图片(http、data:、别的路径)整条删除。描述去掉 `[`、`]`、`\`,`|` 换成 `/`。
- note 与 translate 失败时 publish 不执行,可手动 retry。

### AI CLI 固定调用

```text
claude -p --model <必填> --tools "" --input-format stream-json --output-format stream-json --verbose
       --max-turns 1 --no-session-persistence --strict-mcp-config --setting-sources project
```

- stdin 是一行 stream-json 的 user 消息:先是 prompt 文本块,每张图前再加一个 `[N]` 文本块,图为 base64 image 块,media_type 按文件头取 image/jpeg、image/png、image/gif 或 image/webp。没有图时也走这一条路径。工具全关,不开 Read。不能用 `--bare`,它不读 OAuth。
- cwd:claude 是每次调用前清空重建的 `$HOME/ai-cwd`(固定目录,宿主 `~/.claude/projects` 下只多一项);codex 是每次新建的空目录。
- 成功判据:stdout 的 stream-json 事件里 type=result 的那一条 is_error=false、subtype=success、result 非空,且退出码 0。退出码非 0 时也解析这条事件再分类。没有 result 事件时按 stderr、stdout 里不是 JSON 的行与各事件的 error 字段分类,stderr 在前;assistant 正文不参与分类。
- 失败分类:凭据失效 → 30;限流和超时 → 10;其它 → 20。
- 调用记录:有 FLORI_AI_LOG_DIR 时,每次走到启动 CLI 的调用在其中写 `<step>-a<attempt>-<k>.*`。step 与 attempt 取 FLORI_STEP、FLORI_ATTEMPT,k 是本次尝试里从 1 起第一个未被占用的号。
  - 调用前写 prompt.md 与 call.json。prompt.md 是发出的文字原样,图片块写成 `<image 路径>`,claude 的 `[N]` 标签照写。call.json 有 tool、model、cli_version(取 job.json)、argv、images(每张图所示 job 文件的相对路径,送的是缩小副本时也写原图)与 started。
  - 调用后补全 call.json:finished、duration_ms、timed_out、exit_code、signal、outcome(ok|failed)、error_class(auth|retry|permanent)与 error。stdout 原样写成 out.json(非空且每个非空行都是 JSON 时)或 out.txt,stderr 末尾 16 KiB 写成 err.txt。不论回复之后能否通过校验都写;translate 带反馈的重问是另一次调用。
  - 记录不含环境变量,所以没有令牌和代理口令。写失败只在 job.log 记一行,不影响步骤。model 为空、图片被拒的调用不启动 CLI,也不写;CLI 启动失败(如不在 PATH)时照常写,exit_code 为 null,out.txt 为空;被 SIGTERM 中止的调用只留调用前的两个文件。
  - translate 缓存命中的块不调用、不写记录,只在 job.log 记一行。加标点对采用的每处同音替换和还原的每处改动各记一行 `punctuate [MM:SS] corrected|reverted "原文" -> "回复"`,回复用不上、保留原文的节记 `kept as transcribed`;这是从 AI 回复里取用了什么的记录。
- codex:`--ask-for-approval never exec --strict-config --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only --ephemeral --color never --json [--image <file>]… --model M -c history.persistence="none" -c web_search="disabled" -C <空目录> --output-last-message <file> -`。`--image` 每张图一次,放在 `--model` 之前,因为它会吞掉后面的多个值。

## 契约 2:vault 布局与 frontmatter

```text
$DATA/vault/<FLORI_VAULT_NAME>/<domain>/<collection | unsorted>/<YYYY-MM-DD>-<bili|yt|arxiv>-<source_id>/
  <标题>.md            AI 笔记,带 frontmatter;末尾有"附属文件"一节
  transcript.md        视频:转写 + 关键帧(非中文时为双语,中文时由 AI 加标点)
  original.md          arXiv:原文 Markdown
  translation.md       arXiv:中文译文(非中文时)
  assets/
```

- 日期取 job.json 的 created(+08:00),retry 不会换文件夹。arXiv 的 source_id 去掉版本号,`/` 换成 `_`,如 `hep-th_9711200`。
- 主笔记用标题命名。标题清洗:NFC,去掉 `\/:*?"<>|#^[]` 与控制字符,压缩空白,去掉首尾的点(点开头在 Obsidian 里是隐藏文件),不超过 100 UTF-8 字节,截断不切开字符。清洗结果为空时用 source id;与 transcript、original、translation、assets 忽略大小写相同时,文件名为 `<结果> 笔记.md`。
- 发布:删除残留的 publish.tmp 后重新组装;`mkdir -p` 父目录后 rename 到最终路径。目标已存在且主笔记 frontmatter 的 job_id 等于本 job 的字符串 id → 视为上次已发布,补写 manifest 后退出 0;frontmatter 没有 job_id 的旧笔记改比 job_url;其它"已存在"退出 20。同一文件系统内 rename 是原子的。
- 文件夹名建好后不能改,因为 Remotely Save 把改名当成删除加新增。服务端从不修改或删除 vault 里已有的内容。
- 视频文件不进 vault,留在 jobs/<id>/,网页详情页可播放。

| frontmatter 字段 | 值 |
|---|---|
| title、aliases | 标题(中文已转成简体);aliases 先是 title,与 video-fetch 或 arxiv-fetch 的 meta.title(平台原标题)不同时再加上原标题 |
| description | 笔记"一句话总结"一节的纯文本,单行;没有这一节或为空时不写 |
| source_url | 原始链接 |
| platform、source_id | bilibili/youtube/arxiv 与平台 ID |
| uploader、published、duration_min、transcript | 视频:video-fetch 的 uploader、published(YYYY-MM-DD)与 duration_ms 四舍五入的整分钟数(不足半分钟记 1);transcript 是 video-transcribe 的来源,manual(人工字幕)、ai(B站 AI 字幕)或 whisper |
| authors、published、arxiv_version、arxiv_category | arXiv:arxiv-fetch 的 authors(数组)、published、version 与 primary_category |
| domain、collection | slug;无集合为 "" |
| created | ISO 时间 +08:00 |
| status | unread |
| language、translated | 取 translate 的 meta;未排 translate 时为 unknown、false |
| tags | [flori, <platform>, <domain>] |
| job_id | job 的字符串 id,见下 |
| job_url | $FLORI_PUBLIC_URL/jobs/<整数 id>;只作跳回网页的链接,地址或整数 id 变了不影响判重 |
| ai_tool、ai_model | 生成笔记所用 |

- 附属文件:同目录里有 transcript.md、original.md、translation.md 时,主笔记末尾加一节 `## 附属文件`,每个文件一行标准 Markdown 相对链接(`- [转写](transcript.md)`、`[原文]`、`[译文]`),不用 wikilink;一个都没有时不加这一节。只写进 vault 里的主笔记,job 目录的 note.md 不变。
- description:取笔记里标题文字是"一句话总结"的那一节(任意级别,提示词要求 `## 一句话总结`),到下一个标题为止。各行去掉行首的引用或列表标记后用空格连成一行;链接只留文字,图片去掉,去掉 `*`、反引号、`~~` 与反斜杠转义,压缩空白。程序抽取,不调用 AI。
- job 的字符串 id:`<bili|yt|arxiv>-<source_id>-<短 hash>`,如 `yt-dQw4w9WgXcQ-edaaa1`;前缀与 source_id 的写法同文件夹名。短 hash 是 sha256("<platform>:<source_id>:<created>") 的前 6 位小写十六进制,三个值取 job.json 的 source.platform、source.id(不换 `/`)与 created 的原字符串,所以只凭 job.json 就能重算(`pipeline::job_key`,只定义一次)。它与 job.json 的 job_id 不是一回事:后者是整数主键,即 jobs/<n>/ 目录名与 /jobs/<n> 地址,清库后从 1 重来;字符串 id 不随之变。
- 来源字段紧跟 source_id,值缺失或为空时不写该字段。文件夹日期与 created 仍取 job 创建时间,不取 published。

## 契约 3:HTTP API

18 个操作,按方法加路径计。请求与响应的字段以 `docs/openapi.json` 为准,它由 `src/api.rs` 生成,`just openapi` 更新。

| 端点 | 说明 |
|---|---|
| GET/POST /api/domains,DELETE /api/domains/{id} | name 即 slug `^[a-z0-9][a-z0-9-]{0,47}$`,不可改;不合法 400 invalid_name;重名 409;仍有集合或 job 时删除 409 |
| GET/POST /api/collections,DELETE /api/collections/{id} | 同上,属于某个领域,同一领域内不重名;`unsorted` 保留给无集合的 job,不能用作集合名(400 invalid_name);领域不存在 404;有 job 时删除 409 |
| PUT /api/collections/{id}/feed | {url\|null};解析为 6 种 kind 之一并规范化后存 feed_url;解析不了 400 invalid_url;null 解绑 |
| GET /api/collections/{id}/feed?limit=30 | 同步调用 feed.py(limit 1–100,超时 60s),每条附 job:{id,status}\|null;未绑定 400 invalid_request;已有刷新在跑或上次刷新开始不到 10s(失败或中途断开的刷新同样计时)409;上游失败 502 upstream,message 取 feed.py stderr 最后一行 |
| POST /api/collections/{id}/feed/jobs | {items:[{platform,source_id}], translate?};每批 1–20 条,超出 400 invalid_request;服务端校验 ID 并自己拼 URL;一个事务内插入;返回 {created, skipped},每项带 job |
| POST /api/jobs | {url, domain_id, collection_id?, translate?};识别不了 400 invalid_url;领域或集合不存在 404;重复 409 conflict 并带 job_id;成功 201 |
| GET /api/jobs?limit= | 按 id 倒序,默认 200 条;筛选在前端做。每项的 id 是整数主键,用于所有 /api/jobs/{id} 端点;job_key 是契约 2 的字符串 id(frontmatter 的 job_id),由 platform、source_id、created 现算,不建列,没有端点接受它。author(视频的 uploader,论文的 authors 用 ", " 连接)、published(YYYY-MM-DD)、duration_ms(视频)、description(publish 的 meta.description)在 publish 成功时由服务端从 video-fetch 或 arxiv-fetch 与 publish 的 manifest.meta 写进 jobs 的同名列,只供列表与筛选;此前、来源没给或早于这些列发布的任务为 null,不回填 |
| GET /api/jobs/{id} | job、steps、files、assets、preview_base、vault_path、obsidian_uri、job_url、ai(job.json 的 ai,读不到时为 null);files 是各步 manifest.files 按 publish 规则合并的结果;每步带 meta(成功步骤的 manifest.meta 原样,其余为空对象)与 files(本步自己的 manifest.files,不受后续步骤覆盖,路径相对 job 目录),以及 manifest 的 skipped_reason,translate 给中文视频加了标点时另有 punctuation{sections, kept_original, corrected, reverted}(早于同音纠错的任务后两项为 0);每步另带 ai_calls[{name, attempt, index, files}],即 ai/ 里本步各次尝试的调用记录,按尝试与序号排序,files 是 job 内相对路径,按 prompt.md、call.json、out.json\|out.txt、err.txt 排列,失败的步骤也列出;记录里是不可信的转写与模型文字,前端只按纯文本显示,经 files 端点按需读取;obsidian_uri 由 publish 的 vault_path 去掉开头的 vault 名、拼上 note_file 得到 |
| GET /api/jobs/{id}/logs?offset= | 返回 job.log 从 offset 起的完整行(每次最多约 256KiB;单行超过上限时按字符边界切开)与 next_offset;offset 超出文件末尾 400 invalid_request;前端每 1.5s 轮询 |
| POST /api/jobs/{id}/retry、/cancel | retry 只对 failed 与 canceled,从失败或取消的那一步继续;cancel 对 queued 与 running,杀进程组;其它状态 409 |
| GET /api/jobs/{id}/files/{*path} | canonicalize 后必须在 job 目录内,否则 404;支持 HTTP Range;所有响应只带一个 `Content-Security-Policy: sandbox allow-same-origin; frame-ancestors 'none'` 与 `X-Content-Type-Options: nosniff`:外部 HTML 不能运行脚本或提交表单,它引用的图片仍按同源加载 |
| GET /api/system | 只读的运行状况,供"关于与系统"页。响应是手写的白名单结构体 SystemInfo,从不序列化整个配置;密钥与可能带口令的值只报有无。version、commit(构建时由 `just image` 经 FLORI_BUILD_COMMIT 注入的短提交号,其它构建为 unknown);ai{tool, model_note, model_translate, cli_version}(当前配置,即新任务会用的);bili_login(是否配置了 FLORI_BILI_SESSDATA,不代表仍有效)、proxy(是否配置了 FLORI_PROXY);ytdlp_version;whisper_model(模型目录的最后一段);data_dir、vault_name;disk_free_bytes、disk_total_bytes(data_dir 所在文件系统,statvfs 失败时为 null);domains、collections、notes(succeeded 的任务数);weekly_goal(3)与 weeks[{start, submitted, met}]:本周与此前三周,新的在前,start 是周一(+08:00),submitted 是该周创建的任务数(不论结果),met 即 submitted ≥ weekly_goal。cli_version 与 ytdlp_version 在服务启动时各取一次 `--version`,请求时不起子进程,取不到为 unknown,接口不因此失败 |
| GET /api/health | {version};唯一不要求认证的端点 |

- 认证:Basic Auth,用户名 FLORI_WEB_USER,口令 FLORI_WEB_PASSWORD,服务端常量时间比较,不存哈希。静态页面和 /api 都要认证。
- 认证失败:
  - 不带 `Authorization` 的请求是浏览器的第一次请求,直接 401,不记日志也不计数。
  - 带了 `Authorization` 但不对时,stderr 记一行:方法、路径(不含查询串)和 Host,各截到 200 字符,不记凭据。
  - 失败按 60s 窗口全局计数,不按来源:外网请求都来自 ECS 同一个地址。窗口内超过 10 次后,每次失败响应延迟 2s 再返回。不锁定,正确口令不计数也不延迟。
- 跨站与嵌入:浏览器给跨站请求和框架里的页面也带上缓存的 Basic Auth,所以服务端在认证之前对下列请求返回 403:
  - `Sec-Fetch-Dest` 为 iframe、frame、embed 或 object,不论方法与路径。
  - `/api` 下 `Sec-Fetch-Site` 为 cross-site 或 same-site,不论方法。
  - 其它路径下 `Sec-Fetch-Site` 为 cross-site 或 same-site,且不是顶层导航(`Sec-Fetch-Mode: navigate` 与 `Sec-Fetch-Dest: document`)。笔记里的 job_url 因此仍能打开。
  - GET/HEAD 以外的方法,`Sec-Fetch-Site` 不是 same-origin 或 none,或 `Origin` 与 `Host` 不符。
  - 不带 `Sec-Fetch-*` 的请求(curl、局域网 http)只受最后一条约束。
- 响应头,含 401、403 与 /api/health:
  - 所有响应带 `X-Frame-Options: DENY` 与 `Referrer-Policy: no-referrer`。在它之下原生表单 POST 的 `Origin` 是 null,会被上面的 Origin 检查拒绝,所以前端的写操作只用 fetch。
  - 静态页面与资源带 `Content-Security-Policy: default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`。markdown-it 的表格对齐输出 style 属性,所以只有样式允许 inline;笔记里其它主机的图片不加载。
  - /api 下的响应带 `Cache-Control: no-store`;响应自己没有 CSP 时带 `Content-Security-Policy: frame-ancestors 'none'`。
  - 每个响应只有一个 CSP 值:浏览器会同时执行多个 CSP 头,不会合并。
- 401 与 403 不在错误码集合里,也不写进 OpenAPI。
- 错误体 `{code, message, job_id?}`。code 是封闭集合:invalid_url、invalid_name、invalid_request(400),not_found(404),conflict(409),upstream(502)。job_id 只在重复投递的 conflict 里出现。
- 请求体不允许未知字段。
- 去重:`jobs_source_once` 唯一索引 (domain_id, ifnull(collection_id, 0), platform, source_id)。arXiv 的 v1、v2 链接归为同一 source_id。同一链接可以分别投到不同领域或集合。
- 状态:
  - job:queued → running → succeeded | failed | canceled。retry 把 failed、canceled 改回 queued。running 表示 job 已进入执行窗口(同时最多 3 个,即全局名额加 1),包括在窗口内等名额与等重试间隔的时间;窗口外的保持 queued。
  - step:pending → running → succeeded | failed | canceled。retry 只把 failed、canceled 的步改回 pending。
  - running 只由执行器写。retry 与 cancel 由 HTTP 层在一个事务里改状态,再调用 `ExecHandle::wake()` 或 `cancel(job_id)`。
  - 服务启动时把 running 的 job 改回 queued、running 的步改回 pending,并删除残留的 `.tmp` 目录。
- 时间字段都是 RFC 3339,时区 +08:00。
- 非 /api 路径返回静态文件,找不到时返回 index.html;前端手工解析 pathname,不用 vue-router。详情页的标签页是真实链接:`/jobs/<id>` 是 AI 笔记,`/jobs/<id>/<逻辑名>` 是该 .md 文件,`/jobs/<id>/meta` 是元信息;不认识的名字显示 AI 笔记,地址不变。`/about` 是只读的关于与系统页,关于部分是随前端发布的静态文字,系统部分读 GET /api/system。/api 下没有的端点(含 /api 与 /api/ 本身)返回 404 not_found,页面不会在 /api 的 CSP 下打开。
- feed.py:`python3 -I $FLORI_STEPS_DIR/feed.py list <kind> <normalized_url> --limit N`,超时 60s。stdout `{"title":str,"items":[{"platform":"bilibili"|"youtube","source_id":str,"title":str,"published":"YYYY-MM-DD"|null,"duration_s":int|null}]}`;退出码沿用契约 1。
  - kind 封闭,规范 URL 由服务端解析投递链接后生成,feed.py 只接受下表形式(整串匹配),其它一律退出 20:

    | kind | 规范 URL |
    |---|---|
    | bili_up | `https://space.bilibili.com/<mid>` |
    | bili_season | `https://space.bilibili.com/<mid>/lists/<season_id>?type=season` |
    | bili_series | `https://space.bilibili.com/<mid>/lists/<series_id>?type=series` |
    | bili_fav | `https://www.bilibili.com/medialist/detail/ml<media_id>`(收藏夹链接不一定带 mid) |
    | yt_channel | `https://www.youtube.com/@<handle>/videos`,或把 `@<handle>` 换成 `channel/<id>`、`c/<name>`、`user/<name>` |
    | yt_playlist | `https://www.youtube.com/playlist?list=<id>` |

  - B站用 4 个公开 JSON 接口,带 UA 与 Referer,匿名可用;遇到 HTTP 412 或 code -352、-412 时等 5s 重试 1 次。SESSDATA 只在 bili_fav 的请求里发给 api.bilibili.com,其余三种公开列表匿名请求,频繁刷新触发的风控只落在 IP 上。
  - YouTube 用 `yt-dlp --flat-playlist --playlist-end N --extractor-args youtubetab:approximate_date -J --proxy $FLORI_PROXY`。
  - 除 yt_playlist 按列表顺序、bili_fav 按收藏时间外,其余按发布时间新的在前。
- 内部接口:`AppState{db, exec, config}`;`ExecHandle::{wake(), cancel(job_id)}`;`pipeline::plan(pipeline, translate) -> Vec<StepSpec>`。
