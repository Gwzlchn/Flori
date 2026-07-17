"""把不同媒介解析为统一 Document Model。"""

from .scholarly_html import parse_scholarly_html
from .scholarly_pdf import parse_pdf_document

__all__ = ["parse_pdf_document", "parse_scholarly_html"]
