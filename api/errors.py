"""HTTP 错误信封的受信任机器码载体。"""

from __future__ import annotations

from fastapi import HTTPException


class CodedHTTPException(HTTPException):
    """detail 只放人类可读文本;error_code 由统一信封 handler 写进 error 字段。"""

    def __init__(
        self,
        status_code: int,
        error_code: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
