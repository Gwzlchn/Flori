"""source-adapter 模式的核心接口:SourceItem / 注册表 / 分派 / 命名 helper。

适配器契约(新增适配器按此实现):

适配器是一个 async 函数,签名:
    async def enum(source_id: str, ctx: SourceContext) -> tuple[str | None, list[SourceItem]]
返回 (source_title, items):
  - source_title: 来源的人类可读名(UP 主名 / 频道名 / RSS 标题 / 目录名)。
    拿不到时返回 None(集合命名会回退用 source_id)。
  - items: 该来源当前可见的全部内容项。适配器只管枚举全集,不做去重;
    去重在 sync_collection 层按 ingested_item_ids 做。

用 @register("<source_type>") 装饰即注册。可创建类型由 configs/sources.yaml 声明,
完整性测试要求声明集合与运行时 SOURCE_ADAPTERS 严格一致。

SourceItem 字段:
  - item_id:   该来源内稳定唯一的 ID,用于去重(B站=bvid、youtube=videoId、
               rss=entry id 或 link、local=文件名/路径)。必须稳定:同一内容多次
               枚举要给同一个 item_id,否则会重复建 job。
  - title:     内容标题(可空字符串)。
  - url:       投递给 create_job_core 的 url(下载/抓取入口)。
  - content_type: video / document / audio 之一(决定走哪条 pipeline)。
  - document_kind: document 的业务体裁；其它类型为 None。

SourceContext(ctx)给适配器提供:
  - ctx.bili_cookies: B站 cookie JSON 串(由 sync_collection 从 db.get_credential('bili_cookies') 取),
                      未登录为 None。
  - ctx.db:           可选的 Database 句柄(youtube 适配器经它读 credentials 表的
                      youtube_cookies 做登录态枚举;其余内置适配器未使用)。
ctx 由 sync_collection 构造并传入,适配器不要自己去 import db / 读环境。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional


@dataclass
class SourceItem:
    """一个待入库的内容项(适配器枚举出的最小单元)。"""
    item_id: str          # 来源内稳定唯一 ID(去重键):bvid / videoId / rss entry id / 文件名
    title: str            # 内容标题(可空字符串)
    url: str              # 投递给 create_job_core 的下载/抓取 url
    content_type: str     # video / document / audio
    document_kind: str | None = None


@dataclass
class SourceContext:
    """适配器运行时上下文。由 sync_collection 构造并传入,集中提供凭证/句柄,
    使适配器纯函数化(不自己 import db、不读环境/全局)。"""
    bili_cookies: Optional[str] = None   # B站 cookie JSON 串(db.get_credential('bili_cookies'))
    db: object | None = None             # 可选 Database 句柄(目前内置适配器均未使用;保留给将来需查库的适配器)


# 适配器类型: (source_id, ctx) -> (source_title | None, [SourceItem])
SourceAdapter = Callable[[str, SourceContext], Awaitable[tuple[Optional[str], list[SourceItem]]]]

# source_type -> 适配器函数。@register 在 import 适配器模块时填充。
SOURCE_ADAPTERS: dict[str, SourceAdapter] = {}
SOURCE_ID_NORMALIZERS: dict[str, Callable[[str], str]] = {}

# 来源徽标/标签:统一来自 shared.sources 注册表(唯一事实源),不在此重复定义。
from shared.sources import subscription_badge as source_label  # noqa: E402,F401


def register(source_type: str) -> Callable[[SourceAdapter], SourceAdapter]:
    """装饰器:把适配器函数登记到 SOURCE_ADAPTERS[source_type]。
    重复注册同一 source_type 直接覆盖(便于测试替换 / 热重载)。"""
    def deco(fn: SourceAdapter) -> SourceAdapter:
        SOURCE_ADAPTERS[source_type] = fn
        return fn
    return deco


def register_source_id_normalizer(
    source_type: str, normalizer: Callable[[str], str],
) -> None:
    """登记来源 ID 的规范化与输入边界,供创建集合前 fail-closed 校验。"""
    SOURCE_ID_NORMALIZERS[source_type] = normalizer


def normalize_source_id(source_type: str, source_id: str) -> str:
    """返回来源的持久化规范 ID;未登记类型保持原值。"""
    normalizer = SOURCE_ID_NORMALIZERS.get(source_type)
    return normalizer(source_id) if normalizer else source_id


async def enumerate_source(
    source_type: str, source_id: str, ctx: SourceContext,
) -> tuple[str | None, list[SourceItem]]:
    """按 source_type 分派到注册的适配器,枚举该来源的全部内容项。
    返回 (source_title, items)。未知 source_type 抛 ValueError(调用方转 4xx/记日志)。"""
    adapter = SOURCE_ADAPTERS.get(source_type)
    if adapter is None:
        raise ValueError(f"unsupported source_type: {source_type}")
    return await adapter(source_id, ctx)
