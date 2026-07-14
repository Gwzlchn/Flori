"""来源注册表只读路由。"""

from fastapi import APIRouter, Depends

from api.deps import verify_token
from shared.source_registry import source_catalog
from api.wire_schemas import API_ERROR_RESPONSES, SourceCatalogResponse


router = APIRouter(
    prefix="/api/sources", tags=["sources"], dependencies=[Depends(verify_token)],
    responses=API_ERROR_RESPONSES,
)


@router.get("", response_model=SourceCatalogResponse)
async def list_sources() -> SourceCatalogResponse:
    """返回内容、投递与订阅来源目录;数据来自 configs/sources.yaml。"""
    return SourceCatalogResponse.model_validate(source_catalog())
