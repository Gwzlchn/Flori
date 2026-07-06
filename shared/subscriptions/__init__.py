"""多源订阅 source-adapter 层。

一个"来源"(B站 UP / YouTube 频道 / RSS / 本地目录…)由一个适配器枚举出若干
SourceItem(待入库内容项)。同步逻辑(api/routes/collections.sync_collection)只认
统一接口 enumerate_source(source_type, source_id, ctx),按 source_type 分派到注册
的适配器,与具体来源解耦。

新增适配器只需:
  1. 在 shared/subscriptions/<source>.py 写 `async def enum(source_id, ctx) -> ...`
  2. 用 `@register("<source_type>")` 装饰它(返回 (source_title, [SourceItem]))
  3. 在 shared/sources.py 的 _SUBSCRIPTION 注册表登记 source_type(徽标/集合id标签/slug)
  4. import 该模块以触发注册(见本文件末尾的 eager-import)
"""

from __future__ import annotations

from .base import (  # noqa: F401  (re-export 公共接口)
    SOURCE_ADAPTERS,
    SourceContext,
    SourceItem,
    enumerate_source,
    register,
    source_label,
)

# 触发各适配器模块的 @register 副作用(import 即注册)。新增适配器在此加一行 import。
# 放在文件末尾、re-export 之后,避免适配器模块反向 import 本包时的循环。
# bilibili 一个模块内含 bilibili_up/bilibili_fav/bilibili_collection 三个 @register。
from . import bilibili  # noqa: E402,F401
from . import youtube  # noqa: E402,F401
from . import rss  # noqa: E402,F401  (rss 内 feedparser 惰性 import,缺依赖不影响本行)
from . import local_dir  # noqa: E402,F401
from . import book  # noqa: E402,F401  (book_toc:在线书目录→章节,jupyter-book/sphinx)
