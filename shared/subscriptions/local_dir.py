"""本地目录 source-adapter('local_dir')。

把宿主上某个被监听目录(挂进 api/worker 容器,默认约定 /data/inbox)当作订阅来源:
递归扫描其中支持的扩展名文件,每个文件枚举为一个 SourceItem,由 sync_collection
建 job 入库。无网络下载——01_download 识别 file:// url 后把文件复制进 job 的 input/。

source_id = 被扫描目录的绝对路径。source_title = 目录 basename(命名层用于
<名>-local)。

去重键 item_id = "相对路径|大小|mtime秒":
  - 相对路径稳定标识"同一个文件"(目录内移动/重命名视作新文件,符合直觉);
  - 叠加 size+mtime,使文件被原地修改/替换后 item_id 变化 → 重新入库(取最新内容)。
    内容未变时三者皆同,item_id 稳定,不会重复建 job(去重在 sync_collection 层做)。

扩展名 → content_type(决定 pipeline):
  .pdf                          -> paper
  .mp4/.mkv/.webm/.mov          -> video
  source_detect.AUDIO_SUFFIXES  -> audio
  .md/.txt/.html                -> article
其它扩展名忽略(不枚举),避免把无关文件灌进 pipeline。
"""

from __future__ import annotations

import os
from pathlib import Path

from shared.source_detect import AUDIO_SUFFIXES
from shared.subscriptions.base import SourceContext, SourceItem, register

# 扩展名(小写,含点)-> content_type。键集合即"支持的扩展名"。
# 音频扩展名从 source_detect.AUDIO_SUFFIXES 派生,与 detect_source/rss 单一事实源对齐。
EXT_CONTENT_TYPE: dict[str, str] = {
    ".pdf": "paper",
    ".mp4": "video",
    ".mkv": "video",
    ".webm": "video",
    ".mov": "video",
    ".md": "article",
    ".txt": "article",
    ".html": "article",
    **{ext: "audio" for ext in AUDIO_SUFFIXES},
}


def scan_dir(root: str) -> list[SourceItem]:
    """递归扫描目录,把支持的扩展名文件枚举为 SourceItem 列表(纯函数,无 IO 副作用外的状态)。

    单列出来便于测试 monkeypatch(适配器经模块属性 local_dir.scan_dir 调用)。
    目录不存在 / 非目录 → 返回空列表(不抛,sync 当作"暂无内容")。"""
    base = Path(root)
    if not base.is_dir():
        return []
    abs_base = base.resolve()
    items: list[SourceItem] = []
    for dirpath, _dirnames, filenames in os.walk(abs_base):
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            content_type = EXT_CONTENT_TYPE.get(ext)
            if content_type is None:
                continue
            fpath = Path(dirpath) / fname
            try:
                st = fpath.stat()
            except OSError:
                continue  # 扫描中文件被删/不可读 → 跳过,不中断整体枚举
            rel = os.path.relpath(fpath, abs_base)
            item_id = f"{rel}|{st.st_size}|{int(st.st_mtime)}"
            items.append(SourceItem(
                item_id=item_id,
                title=fname,
                url=f"file://{fpath}",
                content_type=content_type,
            ))
    return items


@register("local_dir")
async def enumerate_local_dir(
    source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """枚举本地目录(source_id=绝对路径)下全部受支持文件 → SourceItem 列表。

    经模块属性 scan_dir 调用(便于测试 monkeypatch 'shared.subscriptions.local_dir.scan_dir')。
    source_title = 目录 basename(空路径/根目录回退 None,由命名层用 source_id 兜底)。
    不做去重——去重在 sync_collection 层按 ingested_item_ids 做(契约要求枚举全集)。"""
    from shared.subscriptions import local_dir  # 经模块属性调用,使 monkeypatch 生效

    items = local_dir.scan_dir(source_id)
    source_title = os.path.basename(os.path.normpath(source_id)) or None
    return source_title, items
