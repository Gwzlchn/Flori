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
- 按步骤名找前序产物:`steps/*-<name>/manifest.json`(`pipeline::find_step`)。
- translate=off 时不排入 translate。auto 时总是排入;判为中文或 unknown 时照常建目录,manifest 只带 `meta.skipped_reason`。
- 正文图片一律写 `assets/<basename>`。publish 按步骤顺序合并 manifest.files,同名时后面的步骤覆盖前面的,只允许 translate 覆盖 transcript.md;assets 重名按 20 处理;translate 不输出 assets,只引用 video-frames 的 assets。
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
| note、translate | HTTPS_PROXY、HTTP_PROXY = $FLORI_PROXY;CLAUDE_CODE_DISABLE_CLAUDE_MDS=1;DISABLE_AUTOUPDATER=1;DISABLE_UPDATES=1;CLAUDE_CODE_OAUTH_TOKEN(存在时);CODEX_HOME(tool=codex 时);translate 另加 FLORI_CACHE_DIR |
| feed.py(不是步骤,同样 env_clear) | FLORI_PROXY(只经 yt-dlp `--proxy` 使用,B站请求显式不走代理)、FLORI_BILI_SESSDATA |

### 步骤表

| 步骤 | 资源 | 输出 | 超时 | 尝试 |
|---|---|---|---|---|
| video-fetch | net | video.mp4(发布后保留在 job 目录)、可选 subtitle.srt;meta: title, uploader, duration_ms, published | 30m | 3 |
| video-transcribe | whisper | transcript.json;字幕优先;meta: origin, language, language_probability | 2h | 2 |
| video-frames | cpu | assets/frame-<ms>.jpg、sections.json `[{start_ms, frames:[basename], text}]`(30s 一节)、由它渲染的 transcript.md | 1h | 2 |
| arxiv-fetch | net | page.html、资源原件;meta: arxiv_id(去版本号), version, title, authors, abstract, html_source(arxiv\|ar5iv) | 15m | 3 |
| arxiv-convert | cpu | original.md、assets/;公式取 MathML alttext 写成 `$…$`/`$$…$$` | 10m | 2 |
| note | ai | note.md(读原文,中文输出,只有正文);单次调用超时 1800s | 35m | 1 |
| translate(auto 时排入) | ai | arXiv:translation.md;视频:读 sections.json 写双语 transcript.md;每块调用超时 600s | 3h | 1 |
| publish | local | meta.vault_path | 5m | 2 |

全局 Semaphore(2) 限制同时运行的步骤数,whisper 与 ai 各另有 Semaphore(1),local 不占全局名额。

- arxiv-fetch:官方 HTML 偶尔返回 200 但内容被截断,必须检查完整闭合;ar5iv 对没有 HTML 的论文返回 200 落地页,必须识别;两者都不可用时退出 20,不回退 PDF。
- translate:
  - 判定:去掉公式、代码、URL、时间戳后,汉字/(汉字+字母) ≥ 0.3 且几乎没有假名和谚文 → 中文,跳过;可比较的字母不足 200 个 → unknown,跳过。判定函数只在 Rust 里实现一次。
  - arXiv:按空行切块,每块约 12k 字符;`$$…$$`、代码、表格、图片行、References 原样保留;行内公式、代码、URL、引用号换成占位符,译完还原;不校验数字。
  - 视频:按 sections.json 的节翻译,每节写成"关键帧 + 中文 + 折叠的原文"(`> [!quote]- 原文`)。
  - 串行;每块结果按 sha256(prompt‖model‖payload) 缓存,手动重试只补未缓存的块;校验失败时带反馈再问 1 次,仍失败退出 10。
  - 超过 25 块不调用 AI:只写 meta.skipped_reason=too_long 与块数,退出 0;笔记与原文照常发布,详情页显示原因。
- note 与 translate 失败时 publish 不执行,可手动 retry。

### AI CLI 固定调用

```text
claude -p --model <必填> --output-format json --tools "" --max-turns 1 --no-session-persistence
       --strict-mcp-config --setting-sources project
```

- prompt 走 stdin,cwd 为每次调用新建的空目录。不能用 `--bare`,它不读 OAuth。
- 成功判据:退出码 0、is_error=false、subtype=success、result 非空。退出码非 0 时也解析 stdout 的 result 再分类。
- 失败分类:凭据失效 → 30;限流和超时 → 10;其它 → 20。
- codex:`--ask-for-approval never exec --strict-config --ignore-user-config --ignore-rules --skip-git-repo-check --sandbox read-only --ephemeral --color never --json --model M -c history.persistence="none" -c web_search="disabled" -C <空目录> --output-last-message <file> -`。

## 契约 2:vault 布局与 frontmatter

```text
$DATA/vault/<FLORI_VAULT_NAME>/<domain>/<collection | unsorted>/<YYYY-MM-DD>-<bili|yt|arxiv>-<source_id>/
  <标题>.md            AI 笔记,带 frontmatter
  transcript.md        视频:转写 + 关键帧(非中文时为双语)
  original.md          arXiv:原文 Markdown
  translation.md       arXiv:中文译文(非中文时)
  assets/
```

- 日期取 job.json 的 created(+08:00),retry 不会换文件夹。arXiv 的 source_id 去掉版本号,`/` 换成 `_`,如 `hep-th_9711200`。
- 主笔记用标题命名。标题清洗:NFC,去掉 `\/:*?"<>|#^[]` 与控制字符,压缩空白,不超过 100 UTF-8 字节,截断不切开字符。
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
| GET/POST /api/collections,DELETE /api/collections/{id} | 同上,属于某个领域,同一领域内不重名;领域不存在 404;有 job 时删除 409 |
| PUT /api/collections/{id}/feed | {url\|null};解析为 6 种 kind 之一并规范化后存 feed_url;解析不了 400 invalid_url;null 解绑 |
| GET /api/collections/{id}/feed?limit=30 | 同步调用 feed.py(limit 1–100,超时 60s),每条附 job:{id,status}\|null;未绑定 400 invalid_request;已有刷新在跑 409;上游失败 502 upstream,message 取 feed.py stderr 最后一行 |
| POST /api/collections/{id}/feed/jobs | {items:[{platform,source_id}], translate?};每批 1–20 条,超出 400 invalid_request;服务端校验 ID 并自己拼 URL;一个事务内插入;返回 {created, skipped},每项带 job |
| POST /api/jobs | {url, domain_id, collection_id?, translate?};识别不了 400 invalid_url;领域或集合不存在 404;重复 409 conflict 并带 job_id;成功 201 |
| GET /api/jobs?limit= | 按 id 倒序,默认 200 条;筛选在前端做 |
| GET /api/jobs/{id} | job、steps、files、assets、preview_base、vault_path、obsidian_uri、job_url |
| GET /api/jobs/{id}/logs?offset= | 返回 job.log 从 offset 起的完整行(每次最多约 256KiB)与 next_offset;前端每 1.5s 轮询 |
| POST /api/jobs/{id}/retry、/cancel | retry 只对 failed 与 canceled,从失败或取消的那一步继续;cancel 对 queued 与 running,杀进程组;其它状态 409 |
| GET /api/jobs/{id}/files/{*path} | canonicalize 后必须在 job 目录内,否则 404;支持 HTTP Range |
| GET /api/health | {version};唯一不要求认证的端点 |

- 认证:Basic Auth,用户名 FLORI_WEB_USER,口令 FLORI_WEB_PASSWORD,服务端常量时间比较,不存哈希。静态页面和 /api 都要认证。
- 错误体 `{code, message, job_id?}`。code 是封闭集合:invalid_url、invalid_name、invalid_request(400),not_found(404),conflict(409),upstream(502)。job_id 只在重复投递的 conflict 里出现。
- 请求体不允许未知字段。
- 去重:`jobs_source_once` 唯一索引 (domain_id, ifnull(collection_id, 0), platform, source_id)。arXiv 的 v1、v2 链接归为同一 source_id。同一链接可以分别投到不同领域或集合。
- 状态:
  - job:queued → running → succeeded | failed | canceled。retry 把 failed、canceled 改回 queued。
  - step:pending → running → succeeded | failed | canceled。retry 只把 failed、canceled 的步改回 pending。
  - running 只由执行器写。retry 与 cancel 由 HTTP 层在一个事务里改状态,再调用 `ExecHandle::wake()` 或 `cancel(job_id)`。
  - 服务启动时把 running 的 job 改回 queued、running 的步改回 pending,并删除残留的 `.tmp` 目录。
- 时间字段都是 RFC 3339,时区 +08:00。
- 非 /api 路径返回静态文件,找不到时返回 index.html;前端手工解析 pathname,不用 vue-router。
- feed.py:`python3 -I $FLORI_STEPS_DIR/feed.py list <kind> <normalized_url> --limit N`,超时 60s。stdout `{"title":str,"items":[{"platform":"bilibili"|"youtube","source_id":str,"title":str,"published":"YYYY-MM-DD"|null,"duration_s":int|null}]}`;退出码沿用契约 1。
  - kind 封闭:bili_up、bili_season、bili_series、bili_fav、yt_channel、yt_playlist。
  - B站用 4 个公开 JSON 接口,带 UA 与 Referer,匿名可用;遇到 412 或 -352 时等 5s 重试 1 次。SESSDATA 存在时只发给 api.bilibili.com。
  - YouTube 用 `yt-dlp --flat-playlist --playlist-end N --extractor-args youtubetab:approximate_date -J --proxy $FLORI_PROXY`。
  - 除 yt_playlist 按列表顺序外,其余新的在前。
- 内部接口:`AppState{db, exec, config}`;`ExecHandle::{wake(), cancel(job_id)}`;`pipeline::plan(pipeline, translate) -> Vec<StepSpec>`。
