# Flori 契约

本文件是步骤 CLI、vault 布局和 HTTP API 的唯一权威。代码里的类型在 `src/pipeline.rs`(契约 1、2)和 `src/api.rs`(契约 3),改契约时三处在同一提交里一起改。

## 契约 1:步骤 CLI

```text
调用   内置:<current_exe> step <name> <job_dir> <out_dir>
       Python:python3 -I $FLORI_STEPS_DIR/<script>.py <子命令> <job_dir> <out_dir>
job_dir 只读:job.json、steps/<nn>-<name>/{manifest.json, 文件, assets/}
out_dir steps/<nn>-<name>.tmp/,执行器建好的空目录;成功后原子 rename 为 steps/<nn>-<name>/;失败时整体删除,下次尝试重建
cache   只有 translate 拿到 FLORI_CACHE_DIR=jobs/<id>/cache/translate/(可写,跨尝试保留)
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
| video-fetch(youtube) | HTTPS_PROXY、HTTP_PROXY = $FLORI_PROXY |
| video-fetch(bilibili) | FLORI_BILI_SESSDATA(服务端有值时才传);不带代理 |
| video-transcribe | FLORI_WHISPER_MODEL、FLORI_STEPS_DIR |
| publish | FLORI_VAULT_DIR=$DATA/vault |
| note、translate | HTTPS_PROXY、HTTP_PROXY = $FLORI_PROXY;CLAUDE_CODE_DISABLE_CLAUDE_MDS=1;DISABLE_AUTOUPDATER=1;DISABLE_UPDATES=1;CLAUDE_CODE_OAUTH_TOKEN(存在时);CODEX_HOME(tool=codex 时);translate 另加 FLORI_CACHE_DIR |
| feed.py(不是步骤,同样 env_clear) | FLORI_PROXY(只经 yt-dlp `--proxy` 使用,B站请求显式不走代理)、FLORI_BILI_SESSDATA |

### 步骤表

| 步骤 | 资源 | 输出 | 超时 | 尝试 |
|---|---|---|---|---|
| video-fetch | net | video.mp4(发布后保留在 job 目录)、可选 subtitle.srt(转写用的人工字幕,选轨规则见下)、subtitle_zh.srt(中文人工字幕,作译文)与 ai_subtitle_<lang>.srt(B站 AI 字幕,待 video-transcribe 核对语言);meta: title, uploader, duration_ms, published,有 subtitle.srt 时另有 subtitle_language | 30m | 3 |
| video-transcribe | whisper | transcript.json;人工字幕优先,其次与音频语言相同的 B站 AI 字幕,最后 whisper;meta: origin(subtitle\|whisper), language, language_probability,origin 为 subtitle 时另有 subtitle_kind(manual\|ai) | 2h | 2 |
| video-frames | cpu | assets/frame-<ms>.jpg、sections.json `[{start_ms, frames:[basename], text}]`(30s 一节,去重后每节至多 2 张关键帧,全片不设总数上限)、由它渲染的 transcript.md | 1h | 2 |
| arxiv-fetch | net | page.html、资源原件;meta: arxiv_id(去版本号), version("vN",拿不到时省略), title, authors, abstract, html_source(arxiv\|ar5iv);官方页截断且没有 ar5iv 时退出 10,两处都确定没有 HTML 时退出 20 | 15m | 3 |
| arxiv-convert | cpu | original.md、assets/;公式取 MathML alttext 写成 `$…$`/`$$…$$`,其中未转义的 `$`(`\text{}` 里的公式)依次换成 `\(`、`\)`,公式编号去掉 `$`,公式只在自己的定界符处结束;正文文字里的 `[`、`]`、`!` 一律转义,链接、图片与脚注只由 convert 写出,链接地址百分号编码 | 10m | 2 |
| note | ai | note.md(读原文与全部关键帧或至多 10 张论文图,中文输出,只有正文,可用 `![描述](assets/<basename>)` 插图);单次调用超时 1800s | 35m | 1 |
| translate(auto 时排入) | ai | arXiv:translation.md;视频:读 sections.json,非中文写双语 transcript.md,中文写加标点的 transcript.md;每块调用超时 600s | 3h | 1 |
| publish | local | meta: vault_path(相对 FLORI_VAULT_DIR,含 vault 名), note_file(主笔记文件名) | 5m | 2 |

全局 Semaphore(2) 限制同时运行的步骤数,whisper 与 ai 各另有 Semaphore(1),local 不占全局名额。

### manifest.files 逻辑名

后续步骤和 publish 只按逻辑名找文件,不猜文件名。

| 步骤 | files | assets |
|---|---|---|
| video-fetch | video、subtitle(有可用于转写的人工字幕时,选轨规则见下)、subtitle_zh(原声确定不是中文且有中文人工字幕时)、ai_subtitle_<lang>(B站没有 subtitle 而有该语言的 AI 字幕时) | 无 |
| video-transcribe | transcript_json | 无 |
| video-frames | transcript、sections | frame-<ms>.jpg |
| arxiv-fetch | page | 无;资源原件只作为普通文件留在步骤目录 |
| arxiv-convert | original | 正文图片 |
| note | note | 无;正文引用 video-frames 或 arxiv-convert 的 assets |
| translate | arXiv:translation;视频:transcript(覆盖 video-frames 的) | 无 |
| publish | 无 | 无 |

- 进 vault 的逻辑名只有 note、transcript、original、translation:note 写成 `<标题>.md`,其余写成 `<逻辑名>.md`;合并后的 assets 放进 `assets/`。其余逻辑名只留在 job 目录。
- 标题取步骤顺序中第一个非 null 的 manifest.title。
- transcript.json:`{"language": str|null, "segments": [{"start_ms": int, "end_ms": int, "text": str}]}`。
- sections.json 的 text 是未转义的原文;写进 Markdown 的一方(video-frames 的 transcript.md、translate 的双语版与加标点版)统一用 `pipeline::escape_markdown` 转义。Section 类型、取前序文件(`pipeline::step_file`)与节时间格式(`pipeline::clock`)只在 src/pipeline.rs 定义一次。
- translate 的 meta 总有 language 与 translated(bool)。language 在视频路径沿用 video-transcribe 的 meta.language(为 unknown 时改用判定值);arXiv 取判定值:zh、ja、ko、en(拉丁字母为主;arXiv 论文几乎都是英文,与视频的 en 保持一致)、other(其它文字)或 unknown。跳过时另有 skipped_reason(chinese、unknown、too_long;chinese 只出现在 arXiv),too_long 时另有 chunks(int)。译出时另有 translation_source:ai(调用 AI)或 subtitle(视频用 video-fetch 的 subtitle_zh)。中文视频加了标点时 translated 为 false,另有 punctuated(true)、sections(送去加标点的有文字的节数,int)与 kept_original(校验未过、保留原文的节数,int)。

- video-fetch 的字幕:取人工字幕与 B站 AI 字幕,不取 YouTube 自动字幕;尽力而为,失败只记日志,由 whisper 转写。
  - 原声语言取 yt-dlp 的 language 的主子标签(`en-US` 记 en);不是纯小写字母标签时按未知处理。yt-dlp 的 B站提取器不给 language,B站总按未知处理,即中文优先。
  - subtitle.srt:原声已知时只取原声语言的轨,原声为中文时没有中文轨再用英文;原声未知时中文优先,其次英文。同一语言内简体先于繁体,不带地区的标签先于带地区的。
  - subtitle_zh.srt:只在原声确定不是中文时写,取最好的中文轨。原声未知时中文轨可能就是原话,不当译文。
  - `--sub-langs`:原声是 zh、en 以外的已知语言时为 `<原声>.*,zh.*`(英文轨用不上,而任一语言下载失败会中止整次字幕下载);其余情况为 `zh.*,en.*`。B站另按同样的语言加 `ai-<lang>`,即 `zh.*,en.*,ai-zh,ai-en`。
  - B站 AI 字幕 `ai-<lang>`:只对登录的 SESSDATA 列出,没有 SESSDATA 时照旧只有人工字幕。<lang> 是原声时它是语音识别,否则是机器翻译,B站不标明是哪种。没有 subtitle.srt 时才保留为 ai_subtitle_<lang>.srt,有人工字幕时删除;从不作 subtitle.srt 或 subtitle_zh.srt。
- video-transcribe 的来源:有 subtitle 时直接用,subtitle_kind=manual,language_probability 为 1。否则有 ai_subtitle_<lang> 时先用 `whisper.py detect` 判定音频语言(与 whisper 转写时的判定相同,不解码文字):有该语言的 AI 字幕就原样用它,subtitle_kind=ai,language 与 language_probability 取判定值;没有则 whisper 转写。
  - whisper 判定为 zh 时,每个窗口都带一句简体、带标点的中文提示(hotwords),输出再经 OpenCC t2s 转成简体;其它语言不带提示,不转换。
  - 带提示时 whisper 常把整个 30s 窗口出成一段,而 video-frames 按段起点分节,所以 zh 的段在后面紧跟汉字的 `。！？；，,!?;` 处切开,各片按字数比例分时间。切点后是汉字,`push_text` 拼回时不加空格,文字不变。
- arxiv-fetch:官方 HTML 偶尔返回 200 但内容被截断,必须检查完整闭合;ar5iv 对没有 HTML 的论文返回 200 落地页,必须识别;两者都不可用时退出 20,不回退 PDF。
- translate:
  - 判定:去掉公式、代码、URL、时间戳后,汉字/(汉字+字母) ≥ 0.3 且几乎没有假名和谚文 → 中文:arXiv 跳过,视频加标点;可比较的字母不足 200 个 → unknown,跳过。判定函数只在 Rust 里实现一次。
  - 跳过:判为中文的 arXiv 总是跳过;判为 unknown 或超过 25 块时跳过,但非中文视频有 video-fetch 的 subtitle_zh 时不跳过,因为不调用 AI。
  - arXiv:按空行切块,每块约 12k 字符;`$$…$$`、代码、表格、图片行、References 原样保留(文末的脚注定义 `[^n]:` 不算 References,照常翻译);行内公式、代码、URL、引用号(`[12]`,或 arxiv-convert 转义后的 `\[12\]`)与脚注标记 `[^n]` 换成占位符,译完还原;原文里字面的 `⟦` 也作为占位符保护;脚注定义行首的 `[^label]: ` 不送翻译,原样保留;列表项首块不是段落时 arxiv-convert 把标记单独写一行(如 `10.`、`4. -`),下一行缩进到该项内容列时,标记行连同下一行的缩进不送翻译,原样写在该块前且不隔空行;只有列表标记的块原样保留;表格和只含图片的行在最后一个同类行结束,列表里紧跟的下一项不并入;代码围栏按 CommonMark 识别(开围栏的缩进从列表项内容列算起,不设绝对上限;关闭行最多比开围栏深 3 格,同字符、长度不短于开围栏、只含围栏字符);不校验数字。
  - 非中文视频:按 sections.json 的节翻译,每节写成"关键帧 + 中文 + 折叠的原文"(`> [!quote]- 原文`);没有中文的节只写原文,不折叠。译文与原文每行用 `pipeline::escape_markdown` 转义。
    - 有 subtitle_zh 时不调用 AI:每条中文 cue 按起点归入 `start_ms / SECTION_MS * SECTION_MS` 那个 30s 窗口的节(`pipeline::SECTION_MS`,与 video-frames 分节相同)。窗口没有节、或该节没有原文时丢弃这条 cue,第一节之前的 cue 同样丢弃;没有 cue 的节只写原文。
    - 没有 subtitle_zh 时由 AI 逐块翻译,转写不做占位符(只保护字面 `⟦`)。
  - 中文视频:不翻译,也不用 subtitle_zh;按节由 AI 只加标点(`prompts/punctuate.md`),模型、CLI、分节协议、缓存、重问与失败分类都与翻译相同。转写原本带标点时同样处理,字幕和 whisper 的标点都不可靠。
    - 回复要把每个字写回来,所以每块约 6k 字符,是翻译的一半。
    - 逐节校验:回复与原文各自去掉标点和空白后必须完全相同,否则该节保留原文、计入 kept_original,不重问。标点只有 ASCII 的 `, . ; : ? ! " ' ( ) [ ] { }` 与 `，。、；：？！．“”‘’＂＇「」『』（）［］｛｝《》〈〉【】〔〕…⋯—–―·・`;连字符 `-`、`~`、`/`、`%` 等符号都是内容。夹在两个 ASCII 字母或数字之间的 ASCII 标点是数字或词的一部分(3.5、1,000、10:30、Node.js),算内容;两个 ASCII 字母或数字之间的断开(空白或标点)也算内容,单词与数字不能合并或拆开。
    - 通过校验的节把空白压成一个空格。transcript.md 与 video-frames 的版式相同(都用 `video::render_transcript`):每节时间标题、关键帧、用 `pipeline::escape_markdown` 转义的文字,没有原文折叠。
  - 串行;每块结果按 sha256(prompt‖model‖payload) 缓存,手动重试只补未缓存的块;校验失败时带反馈再问 1 次,仍失败退出 10。
  - 超过 25 块不调用 AI:只写 meta.skipped_reason=too_long 与块数,退出 0;笔记与原文照常发布,详情页显示原因。加标点按它自己的块大小计;用 subtitle_zh 翻译的视频不受此限。
- note:
  - 一次调用同时发原文与图,图按序号 [N] 列出,序号即附图顺序。视频按时间顺序发 sections 里的全部关键帧,只受 API 单次请求 600 张图的上限约束,超出时按时间均匀取;每张写时间与该节文字前 150 字;笔记每个要点只要有能说明它的画面就配一张。论文至多 10 张,取 original.md 里有图注的图,按出现位置均匀取:
    - 对象是一串相邻的图片段(中间可夹 "(a) …"、"b) …" 这类子图注段),或一个 Markdown 表格;arxiv-convert 把多面板图的每个面板写成单独一段。
    - 图注段以 "Figure"、"Fig."、"Table"、"图"、"表" 开头。图注归紧挨在它前面的对象(图注在下);前面没有对象或已被别的图注占用时,归紧挨在它后面的对象(图注在上,表格常见)。对象里每张图都用这条图注;正文段把对象隔开。
  - 只发内容确实是 jpeg、png、gif、webp 的图,media_type 按文件头判断,不看扩展名;SVG、扩展名是图片但内容不是的文件跳过。
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
- codex:`--ask-for-approval never exec --strict-config --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only --ephemeral --color never --json [--image <file>]… --model M -c history.persistence="none" -c web_search="disabled" -C <空目录> --output-last-message <file> -`。`--image` 每张图一次,放在 `--model` 之前,因为它会吞掉后面的多个值。

## 契约 2:vault 布局与 frontmatter

```text
$DATA/vault/<FLORI_VAULT_NAME>/<domain>/<collection | unsorted>/<YYYY-MM-DD>-<bili|yt|arxiv>-<source_id>/
  <标题>.md            AI 笔记,带 frontmatter
  transcript.md        视频:转写 + 关键帧(非中文时为双语,中文时由 AI 加标点)
  original.md          arXiv:原文 Markdown
  translation.md       arXiv:中文译文(非中文时)
  assets/
```

- 日期取 job.json 的 created(+08:00),retry 不会换文件夹。arXiv 的 source_id 去掉版本号,`/` 换成 `_`,如 `hep-th_9711200`。
- 主笔记用标题命名。标题清洗:NFC,去掉 `\/:*?"<>|#^[]` 与控制字符,压缩空白,去掉首尾的点(点开头在 Obsidian 里是隐藏文件),不超过 100 UTF-8 字节,截断不切开字符。清洗结果为空时用 source id;与 transcript、original、translation、assets 忽略大小写相同时,文件名为 `<结果> 笔记.md`。
- 发布:删除残留的 publish.tmp 后重新组装;`mkdir -p` 父目录后 rename 到最终路径。目标已存在且主笔记 frontmatter 的 job_url 指向本 job → 视为上次已发布,补写 manifest 后退出 0;其它"已存在"退出 20。同一文件系统内 rename 是原子的。
- 文件夹名建好后不能改,因为 Remotely Save 把改名当成删除加新增。服务端从不修改或删除 vault 里已有的内容。
- 视频文件不进 vault,留在 jobs/<id>/,网页详情页可播放。

| frontmatter 字段 | 值 |
|---|---|
| title、aliases | 原标题;aliases 是只含原标题的数组 |
| source_url | 原始链接 |
| platform、source_id | bilibili/youtube/arxiv 与平台 ID |
| domain、collection | slug;无集合为 "" |
| created | ISO 时间 +08:00 |
| status | unread |
| language、translated | 取 translate 的 meta;未排 translate 时为 unknown、false |
| tags | [flori, <platform>, <domain>] |
| job_url | $FLORI_PUBLIC_URL/jobs/<id> |
| ai_tool、ai_model | 生成笔记所用 |

## 契约 3:HTTP API

17 个操作,按方法加路径计。请求与响应的字段以 `docs/openapi.json` 为准,它由 `src/api.rs` 生成,`just openapi` 更新。

| 端点 | 说明 |
|---|---|
| GET/POST /api/domains,DELETE /api/domains/{id} | name 即 slug `^[a-z0-9][a-z0-9-]{0,47}$`,不可改;不合法 400 invalid_name;重名 409;仍有集合或 job 时删除 409 |
| GET/POST /api/collections,DELETE /api/collections/{id} | 同上,属于某个领域,同一领域内不重名;`unsorted` 保留给无集合的 job,不能用作集合名(400 invalid_name);领域不存在 404;有 job 时删除 409 |
| PUT /api/collections/{id}/feed | {url\|null};解析为 6 种 kind 之一并规范化后存 feed_url;解析不了 400 invalid_url;null 解绑 |
| GET /api/collections/{id}/feed?limit=30 | 同步调用 feed.py(limit 1–100,超时 60s),每条附 job:{id,status}\|null;未绑定 400 invalid_request;已有刷新在跑或上次刷新开始不到 10s(失败或中途断开的刷新同样计时)409;上游失败 502 upstream,message 取 feed.py stderr 最后一行 |
| POST /api/collections/{id}/feed/jobs | {items:[{platform,source_id}], translate?};每批 1–20 条,超出 400 invalid_request;服务端校验 ID 并自己拼 URL;一个事务内插入;返回 {created, skipped},每项带 job |
| POST /api/jobs | {url, domain_id, collection_id?, translate?};识别不了 400 invalid_url;领域或集合不存在 404;重复 409 conflict 并带 job_id;成功 201 |
| GET /api/jobs?limit= | 按 id 倒序,默认 200 条;筛选在前端做 |
| GET /api/jobs/{id} | job、steps、files、assets、preview_base、vault_path、obsidian_uri、job_url;每步带 manifest 的 skipped_reason,translate 给中文视频加了标点时另有 punctuation{sections, kept_original};obsidian_uri 由 publish 的 vault_path 去掉开头的 vault 名、拼上 note_file 得到 |
| GET /api/jobs/{id}/logs?offset= | 返回 job.log 从 offset 起的完整行(每次最多约 256KiB;单行超过上限时按字符边界切开)与 next_offset;offset 超出文件末尾 400 invalid_request;前端每 1.5s 轮询 |
| POST /api/jobs/{id}/retry、/cancel | retry 只对 failed 与 canceled,从失败或取消的那一步继续;cancel 对 queued 与 running,杀进程组;其它状态 409 |
| GET /api/jobs/{id}/files/{*path} | canonicalize 后必须在 job 目录内,否则 404;支持 HTTP Range;所有响应只带一个 `Content-Security-Policy: sandbox allow-same-origin; frame-ancestors 'none'` 与 `X-Content-Type-Options: nosniff`:外部 HTML 不能运行脚本或提交表单,它引用的图片仍按同源加载 |
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
- 非 /api 路径返回静态文件,找不到时返回 index.html;前端手工解析 pathname,不用 vue-router。/api 下没有的端点(含 /api 与 /api/ 本身)返回 404 not_found,页面不会在 /api 的 CSP 下打开。
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
