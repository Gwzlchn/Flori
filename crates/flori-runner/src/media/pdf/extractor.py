import contextlib
import io
import json
import re
import sys
from pathlib import Path

import fitz


PINNED_VERSION = "1.27.2.3"
HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+|abstract$|introduction$|conclusion)", re.I)
FIGURE = re.compile(r"^(?:figure|fig\.)\s*\d+\s*[:.]?", re.I)
TABLE = re.compile(r"^table\s*\d+\s*[:.]?", re.I)


def text_blocks(page):
    blocks = []
    for raw in page.get_text("blocks", sort=True):
        if len(raw) < 7 or raw[6] != 0:
            continue
        text = " ".join(raw[4].split())
        rect = fitz.Rect(raw[:4]) & page.rect
        if text and not rect.is_empty:
            blocks.append((rect, text))
    return blocks


def rect_json(rect):
    return {"x1": rect.x0, "y1": rect.y0, "x2": rect.x1, "y2": rect.y1}


def section_json(pages):
    sections = []
    heading = "Document"
    blocks = []

    def finish():
        nonlocal blocks
        if blocks:
            sections.append(
                {"id": f"section-{len(sections) + 1}", "heading": heading, "blocks": blocks}
            )
            blocks = []

    for page_number, _, page_blocks in pages:
        for rect, text in page_blocks:
            first_line = text.splitlines()[0].strip()
            if HEADING.match(first_line) and len(text) <= 160:
                finish()
                heading = first_line
                continue
            blocks.append({"page": page_number, "bbox": rect_json(rect), "text": text})
    finish()
    if not sections:
        raise RuntimeError("document has no text blocks")
    return sections


def caption_before(blocks, rect, pattern):
    matching = [
        (candidate_rect, text)
        for candidate_rect, text in blocks
        if pattern.match(text) and candidate_rect.y1 <= rect.y0 and rect.y0 - candidate_rect.y1 <= 100
    ]
    return matching[-1][1] if matching else None


def save_clip(page, rect, path):
    rect = rect & page.rect
    if rect.is_empty or rect.width < 1 or rect.height < 1:
        raise RuntimeError("empty extracted region")
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
    pixmap.save(path)


def figures_json(document, pages, output):
    result = []
    seen = []
    for page_number, page, blocks in pages:
        drawings = [item["rect"] & page.rect for item in page.get_drawings()]
        for caption_rect, caption in blocks:
            if not FIGURE.match(caption):
                continue
            candidates = [
                rect for rect in drawings if rect.y1 <= caption_rect.y0 and caption_rect.y0 - rect.y1 <= 240
            ]
            region = fitz.Rect(caption_rect.x0, max(0, caption_rect.y0 - 140), caption_rect.x1, caption_rect.y0)
            if candidates:
                region = candidates[0]
                for candidate in candidates[1:]:
                    region |= candidate
            number = len(result) + 1
            name = f"figures/figure-{number:03d}.png"
            save_clip(page, region, output / name)
            result.append(
                {
                    "id": f"figure-{number}",
                    "page": page_number,
                    "bbox": rect_json(region),
                    "caption": caption,
                    "artifact_name": name,
                }
            )
            seen.append((page_number, region))
        for image in page.get_images(full=True):
            for region in page.get_image_rects(image[0]):
                if region.is_empty or any(p == page_number and region.intersects(old) for p, old in seen):
                    continue
                caption = caption_before(blocks, region, FIGURE)
                if caption is None:
                    continue
                number = len(result) + 1
                name = f"figures/figure-{number:03d}.png"
                save_clip(page, region, output / name)
                result.append(
                    {
                        "id": f"figure-{number}",
                        "page": page_number,
                        "bbox": rect_json(region),
                        "caption": caption,
                        "artifact_name": name,
                    }
                )
                seen.append((page_number, region))
    return result


def tables_json(pages, output):
    result = []
    for page_number, page, blocks in pages:
        with contextlib.redirect_stdout(io.StringIO()):
            tables = page.find_tables().tables
        for table in tables:
            region = fitz.Rect(table.bbox) & page.rect
            rows = table.extract()
            text = "\n".join(" | ".join(cell or "" for cell in row).strip() for row in rows).strip()
            if not text:
                continue
            caption = caption_before(blocks, region, TABLE)
            if caption is None:
                continue
            number = len(result) + 1
            name = f"tables/table-{number:03d}.png"
            save_clip(page, region, output / name)
            result.append(
                {
                    "id": f"table-{number}",
                    "page": page_number,
                    "bbox": rect_json(region),
                    "caption": caption,
                    "text": text,
                    "artifact_name": name,
                }
            )
    return result


def main():
    if fitz.VersionBind != PINNED_VERSION or len(sys.argv) != 4:
        raise RuntimeError("unsupported extractor runtime")
    input_path, output_value, source_artifact_id = sys.argv[1:]
    output = Path(output_value)
    document = fitz.open(input_path)
    if document.needs_pass or document.page_count < 1:
        raise RuntimeError("unsupported PDF")
    pages = []
    for index, page in enumerate(document):
        pages.append((index + 1, page, text_blocks(page)))
    language = "zh" if any("\u4e00" <= char <= "\u9fff" for _, _, blocks in pages for _, text in blocks for char in text) else "en"
    structure = {
        "schema": "flori.document_structure.v1",
        "source_artifact_id": source_artifact_id,
        "language": language,
        "pages": [
            {"page": number, "width_pt": page.rect.width, "height_pt": page.rect.height}
            for number, page, _ in pages
        ],
        "sections": section_json(pages),
        "figures": figures_json(document, pages, output),
        "tables": tables_json(pages, output),
    }
    (output / "document.json").write_text(
        json.dumps(structure, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
