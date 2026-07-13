"""来源注册表只读路由。"""

from fastapi import APIRouter, Depends

from api.deps import verify_token
from shared.source_registry import source_catalog


router = APIRouter(
    prefix="/api/sources", tags=["sources"], dependencies=[Depends(verify_token)],
)


@router.get("")
async def list_sources() -> dict:
    """返回内容、投递与订阅来源目录;数据来自 configs/sources.yaml。"""
    return source_catalog()
