"""把 PDF Figure/Table locator 确定性渲染为可展示的区域制品。"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import socket
import subprocess
import warnings
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from shared.document_contract import validate_document, validate_quality


PDF_RENDER_DPI = 144
MAX_IMAGE_DIMENSION = 20_000
MAX_IMAGE_PIXELS = 64_000_000
_IMAGE_SUFFIXES = {
    "PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp",
    "TIFF": ".tiff", "BMP": ".bmp",
}


class BlankPdfRegionError(ValueError):
    """源PDF指定区域可渲染但没有任何可见内容."""


def _safe_id(value: object) -> str:
    text = str(value or "")
    if not text or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in text):
        raise ValueError("visual id is invalid")
    return text


def _artifact_exists(job_dir: Path, value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    path = job_dir / value
    # SVG 需要单独的 XML/active-content 安全门；未实现前不能仅凭文件存在宣称 complete.
    if path.suffix.lower() == ".svg":
        return False
    try:
        decoded_suffix = _verified_image(path)
    except ValueError:
        return False
    expected_suffix = path.suffix.lower()
    return decoded_suffix == expected_suffix or {
        decoded_suffix, expected_suffix,
    } == {".jpg", ".jpeg"}


def _append_reason(target: dict[str, Any], reason: str) -> None:
    reasons = target.setdefault("reasons", [])
    if reason not in reasons:
        reasons.append(reason)


def _pdf_region(locator: object) -> tuple[int, list[float]] | None:
    if not isinstance(locator, Mapping) or not isinstance(locator.get("pdf"), Mapping):
        return None
    pdf = locator["pdf"]
    page = pdf.get("page")
    bboxes = pdf.get("bboxes")
    if type(page) is not int or not isinstance(bboxes, list) or not bboxes:
        return None
    valid: list[list[float]] = []
    for bbox in bboxes:
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        values = [float(value) for value in bbox]
        if (
            not all(math.isfinite(value) for value in values)
            or values[2] <= values[0] or values[3] <= values[1]
        ):
            continue
        valid.append(values)
    if not valid:
        return None
    return page, [
        min(bbox[0] for bbox in valid),
        min(bbox[1] for bbox in valid),
        max(bbox[2] for bbox in valid),
        max(bbox[3] for bbox in valid),
    ]


def _pdf_region_token(region: tuple[int, list[float]]) -> str:
    page, bbox = region
    payload = f"{page}:" + ",".join(f"{value:.3f}" for value in bbox)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:12]


def _media_pdf_region(
    media: Mapping[str, Any],
    figure: Mapping[str, Any],
    index: int,
    media_items: list[Mapping[str, Any]],
) -> tuple[int, list[float]] | None:
    own = _pdf_region(media.get("source_locator"))
    if own is not None:
        return own
    region = _pdf_region(figure.get("source_locator"))
    caption = str(figure.get("caption") or "")
    if region is None or len(media_items) != 2:
        return region
    page, bbox = region
    roles = [str(item.get("role") or "").strip() for item in media_items]
    explicit_horizontal = bool(
        re.search(r"\bleft\s*:.*\bright\s*:", caption, re.I)
        or (roles[0].casefold().startswith("(a)") and roles[1].casefold().startswith("(b)"))
    )
    dimensions = [
        (float(item.get("width") or 0), float(item.get("height") or 0))
        for item in media_items
    ]
    ratios = [width / height for width, height in dimensions if width > 0 and height > 0]
    region_ratio = (bbox[2] - bbox[0]) / max(bbox[3] - bbox[1], 1.0)
    stacked_rows = bool(
        not explicit_horizontal and len(ratios) == 2
        and sum(ratios) / len(ratios) > region_ratio * 1.35
    )
    if stacked_rows:
        midpoint = (bbox[1] + bbox[3]) / 2
        split = [bbox[0], bbox[1], bbox[2], midpoint] if index == 0 else [
            bbox[0], midpoint, bbox[2], bbox[3],
        ]
        return page, split
    midpoint = (bbox[0] + bbox[2]) / 2
    # left/right caption 的右panel纵轴标签常跨几何中线,给右侧保留少量空间.
    divider = midpoint - (bbox[2] - bbox[0]) * 0.03 if re.search(
        r"\bleft\s*:.*\bright\s*:", caption, re.I,
    ) else midpoint
    split = [bbox[0], bbox[1], divider, bbox[3]] if index == 0 else [
        divider, bbox[1], bbox[2], bbox[3],
    ]
    return page, split


def _render_region(
    source: Path,
    destination: Path,
    *,
    page: int,
    bbox: list[float],
) -> None:
    scale = PDF_RENDER_DPI / 72
    x0, y0, x1, y1 = bbox
    if (
        not all(math.isfinite(value) for value in bbox)
        or x1 <= x0 or y1 <= y0
    ):
        raise ValueError("PDF visual crop has invalid coordinates")
    pixel_width = max(1, round((x1 - x0) * scale))
    pixel_height = max(1, round((y1 - y0) * scale))
    if (
        pixel_width > MAX_IMAGE_DIMENSION or pixel_height > MAX_IMAGE_DIMENSION
        or pixel_width * pixel_height > MAX_IMAGE_PIXELS
    ):
        raise ValueError("PDF visual crop exceeds pixel limit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.tmp")
    command = [
        "pdftocairo", "-png", "-singlefile", "-r", str(PDF_RENDER_DPI),
        "-f", str(page), "-l", str(page),
        "-x", str(max(0, round(x0 * scale))),
        "-y", str(max(0, round(y0 * scale))),
        "-W", str(pixel_width),
        "-H", str(pixel_height),
        str(source), str(temporary),
    ]
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=120,
        )
        # pdftocairo 把末个参数视为输出根名,即使已有后缀仍会追加格式后缀.
        produced = Path(f"{temporary}.png")
        if _verified_image(produced) != ".png":
            raise ValueError("PDF visual renderer produced a non-PNG image")
        with Image.open(produced) as image:
            extrema = image.convert("RGB").getextrema()
        if all(low == high for low, high in extrema):
            raise BlankPdfRegionError("PDF visual renderer produced a blank image")
        os.replace(produced, destination)
    finally:
        Path(f"{temporary}.png").unlink(missing_ok=True)


def _verified_image(path: Path) -> str:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError("visual asset is empty")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = str(image.format or "").upper()
                width, height = image.width, image.height
                if width <= 0 or height <= 0:
                    raise ValueError("visual asset has empty dimensions")
                if (
                    width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS
                ):
                    raise ValueError("visual asset exceeds pixel limit")
                image.verify()
            with Image.open(path) as image:
                image.load()
                if image.width != width or image.height != height:
                    raise ValueError("visual asset dimensions changed during decode")
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("visual asset exceeds pixel limit") from exc
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("visual asset cannot be decoded") from exc
    suffix = _IMAGE_SUFFIXES.get(image_format)
    if suffix is None:
        raise ValueError("visual asset format is unsupported")
    return suffix


def _download_remote_image(url: str, destination_root: Path) -> Path:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("remote visual URL is invalid")
    for candidate in destination_root.parent.glob(f"{destination_root.name}.*"):
        if candidate.name.startswith(f".{destination_root.name}.tmp"):
            continue
        try:
            _verified_image(candidate)
        except ValueError:
            continue
        return candidate
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise ValueError("remote visual host cannot be resolved") from exc
    parsed_addresses = sorted(
        (ipaddress.ip_address(value) for value in addresses),
        key=lambda value: (value.version, value.packed),
    )
    if not parsed_addresses or any(not value.is_global for value in parsed_addresses):
        raise ValueError("remote visual host is not public")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned = ",".join(
        f"[{value}]" if value.version == 6 else str(value)
        for value in parsed_addresses
    )
    resolve = f"{parsed.hostname}:{port}:{pinned}"
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination_root.with_name(f".{destination_root.name}.tmp")
    try:
        result = subprocess.run(
            [
                "curl", "-fsS", "--connect-timeout", "15", "--max-time", "60",
                "--max-filesize", str(50 * 1024 * 1024),
                "--proto", "=http,https", "--proto-redir", "=http,https",
                "--max-redirs", "0", "--noproxy", "*", "--resolve", resolve,
                "-A", "Mozilla/5.0", "-o", str(temporary),
                "--write-out", "%{http_code}", "--", url,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=70,
            text=True,
        )
        status = str(result.stdout or "").strip()
        if not status.isdigit() or not 200 <= int(status) < 300:
            raise ValueError("remote visual HTTP status is not successful")
        suffix = _verified_image(temporary)
        destination = destination_root.with_suffix(suffix)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _source_raster_candidates(job_dir: Path) -> list[tuple[Path, int, int]]:
    candidates: list[tuple[Path, int, int]] = []
    for path in (job_dir / "input").glob("source-*"):
        try:
            _verified_image(path)
            with Image.open(path) as image:
                candidates.append((path, image.width, image.height))
        except (OSError, ValueError):
            continue

    def source_order(value: tuple[Path, int, int]) -> tuple[int, int, str]:
        match = re.match(r"source-(\d+)_(\d+)", value[0].stem)
        if match is None:
            return 10**9, 10**9, value[0].name
        return int(match.group(1)), int(match.group(2)), value[0].name

    return sorted(candidates, key=source_order)


def _claim_source_raster(
    asset: Mapping[str, Any] | None,
    candidates: list[tuple[Path, int, int]],
    claimed: set[Path],
) -> tuple[Path, int, int] | None:
    if asset is None:
        return None
    width = asset.get("width")
    height = asset.get("height")
    if type(width) not in {int, float} or type(height) not in {int, float}:
        return None
    if width <= 0 or height <= 0:
        return None
    expected_ratio = float(width) / float(height)
    matches = [
        (abs(math.log((candidate_width / candidate_height) / expected_ratio)), index, path,
         candidate_width, candidate_height)
        for index, (path, candidate_width, candidate_height) in enumerate(candidates)
        if path not in claimed and candidate_width > 0 and candidate_height > 0
    ]
    matches = [value for value in matches if value[0] <= 0.02]
    if not matches:
        return None
    _error, _index, path, candidate_width, candidate_height = min(matches)
    claimed.add(path)
    return path, candidate_width, candidate_height


def materialize_html_visuals(
    job_dir: Path,
    document: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把 HTML 正文远程图片快照成本地制品；失败项必须显式降级。"""
    updated = deepcopy(dict(document))
    report = deepcopy(dict(quality))
    assets = {
        str(asset.get("asset_id")): asset
        for asset in updated.get("assets", []) if isinstance(asset, dict)
    }
    resolved: dict[str, str | None] = {}
    source_rasters = _source_raster_candidates(job_dir)
    claimed_rasters: set[Path] = set()
    failures = 0
    localized = 0
    for figure in updated.get("figures", []):
        incomplete = False
        for media in figure.get("media", []):
            asset_id = str(media.get("asset_id") or "")
            asset = assets.get(asset_id)
            artifact = media.get("artifact")
            if _artifact_exists(job_dir, artifact):
                _claim_source_raster(asset, source_rasters, claimed_rasters)
                continue
            media["artifact"] = None
            if asset is None:
                incomplete = True
                continue
            local = asset.get("local_path")
            if not local and str(asset.get("state") or asset.get("status")) in {
                "available", "available_local",
            }:
                local = asset.get("path")
            if _artifact_exists(job_dir, local):
                media["artifact"] = local
                _claim_source_raster(asset, source_rasters, claimed_rasters)
                continue
            source_raster = _claim_source_raster(
                asset, source_rasters, claimed_rasters,
            )
            if source_raster is not None:
                raster_path, width, height = source_raster
                rel = raster_path.relative_to(job_dir).as_posix()
                raw = raster_path.read_bytes()
                asset.update({
                    "path": rel, "local_path": rel,
                    "state": "available", "status": "available_local",
                    "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw), "bytes": len(raw),
                    "width": width, "height": height,
                })
                media.update({"artifact": rel, "width": width, "height": height})
                resolved[asset_id] = rel
                localized += 1
                continue
            remote = asset.get("source_url") or asset.get("path")
            if not isinstance(remote, str) or urlparse(remote).scheme not in {"http", "https"}:
                incomplete = True
                continue
            if asset_id not in resolved:
                try:
                    root = job_dir / "assets" / "document" / _safe_id(asset_id)
                    destination = _download_remote_image(remote, root)
                    rel = destination.relative_to(job_dir).as_posix()
                    raw = destination.read_bytes()
                    asset.update({
                        "path": rel, "local_path": rel,
                        "state": "available", "status": "available_local",
                        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                        "size_bytes": len(raw), "bytes": len(raw),
                    })
                    resolved[asset_id] = rel
                    localized += 1
                except (OSError, subprocess.SubprocessError, ValueError):
                    resolved[asset_id] = None
                    failures += 1
            media["artifact"] = resolved[asset_id]
            incomplete = incomplete or media["artifact"] is None
        if incomplete or any(not item.get("artifact") for item in figure.get("media", [])):
            figure["extraction"]["status"] = "degraded"
            reasons = figure["extraction"].setdefault("reasons", [])
            if "html_visual_asset_incomplete" not in reasons:
                reasons.append("html_visual_asset_incomplete")
    unresolved = any(
        not media.get("artifact")
        for figure in updated.get("figures", []) for media in figure.get("media", [])
    )
    if failures or unresolved:
        reasons = report.setdefault("reasons", [])
        if "html_visual_asset_incomplete" not in reasons:
            reasons.append("html_visual_asset_incomplete")
        if report.get("status") == "complete":
            report["status"] = "degraded"
    else:
        reasons = report.setdefault("reasons", [])
        report["reasons"] = [reason for reason in reasons if reason != "html_asset_remote"]
        if report.get("status") == "degraded" and not report["reasons"]:
            report["status"] = "complete"
    report.setdefault("metrics", {})["html_visual_assets_localized"] = localized
    report["metrics"]["html_visual_asset_failures"] = failures
    return (
        validate_document(updated, expected_job_id=str(updated["job_id"])),
        validate_quality(report, expected_job_id=str(updated["job_id"])),
    )


def materialize_pdf_visuals(
    job_dir: Path,
    document: Mapping[str, Any],
    quality: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """渲染所有可定位 PDF 图表；单个失败显式降级但不吞掉 registry 项。"""
    updated = deepcopy(dict(document))
    report = deepcopy(dict(quality))
    source = job_dir / "input" / "source.pdf"
    if not source.is_file():
        return validate_document(updated), validate_quality(report)

    failures: list[str] = []
    rendered = 0
    for figure in updated.get("figures", []):
        attempted = False
        source_blank = False
        render_failed = False
        media_items = figure.get("media", [])
        if not media_items:
            failures.append(f"figure_media_missing:{figure['figure_id']}")
            figure["extraction"]["status"] = "degraded"
            _append_reason(figure["extraction"], "pdf_visual_media_missing")
            continue
        for media_index, media in enumerate(media_items):
            region = _media_pdf_region(
                media, figure, media_index, media_items,
            )
            media_id = _safe_id(media["media_id"])
            prefix = f"assets/document/{_safe_id(figure['figure_id'])}-{media_id}"
            rel = (
                f"{prefix}-{_pdf_region_token(region)}.png" if region else None
            )
            artifact = media.get("artifact")
            if _artifact_exists(job_dir, artifact) and (
                not str(artifact).startswith(prefix) or artifact == rel
            ):
                continue
            media["artifact"] = None
            if region is None:
                failures.append(f"figure_locator_unavailable:{figure['figure_id']}:{media['media_id']}")
                continue
            attempted = True
            try:
                assert rel is not None
                _render_region(source, job_dir / rel, page=region[0], bbox=region[1])
            except BlankPdfRegionError:
                failures.append(f"figure_source_blank:{figure['figure_id']}:{media_id}")
                source_blank = True
                continue
            except (OSError, subprocess.SubprocessError, ValueError):
                failures.append(f"figure_render_failed:{figure['figure_id']}:{media_id}")
                render_failed = True
                continue
            media["artifact"] = rel
            rendered += 1
        if any(not _artifact_exists(job_dir, item.get("artifact")) for item in figure.get("media", [])):
            figure["extraction"]["status"] = "degraded"
            if source_blank:
                _append_reason(figure["extraction"], "pdf_visual_source_blank")
            if render_failed or (attempted and not source_blank):
                _append_reason(figure["extraction"], "pdf_visual_render_incomplete")
            if not attempted:
                _append_reason(figure["extraction"], "pdf_visual_locator_unavailable")
        else:
            reasons = figure["extraction"].setdefault("reasons", [])
            figure["extraction"]["reasons"] = [
                reason for reason in reasons if reason not in {
                    "html_visual_asset_incomplete",
                    "pdf_visual_locator_unavailable",
                    "pdf_visual_render_incomplete",
                }
            ]
            if not figure["extraction"]["reasons"]:
                figure["extraction"]["status"] = "complete"

    for table in updated.get("tables", []):
        representations = table.setdefault("representations", [])
        crop = next((item for item in representations if item.get("kind") == "source_crop"), None)
        if crop is None:
            if table.get("cells"):
                reasons = table["extraction"].setdefault("reasons", [])
                table["extraction"]["reasons"] = [
                    reason for reason in reasons
                    if reason != "pdf_table_crop_locator_unavailable"
                ]
                if not table["extraction"]["reasons"]:
                    table["extraction"]["status"] = "complete"
                continue
            region = _pdf_region(table.get("source_locator"))
            if region is None:
                failures.append(f"table_locator_unavailable:{table['table_id']}")
                table["extraction"]["status"] = "degraded"
                _append_reason(table["extraction"], "pdf_table_crop_locator_unavailable")
                continue
            crop = {
                "kind": "source_crop", "artifact": None,
                "source_locator": table.get("source_locator"),
            }
            representations.append(crop)
        region = _pdf_region(crop.get("source_locator") or table.get("source_locator"))
        prefix = f"assets/document/{_safe_id(table['table_id'])}"
        rel = f"{prefix}-{_pdf_region_token(region)}.png" if region else None
        artifact = crop.get("artifact")
        if _artifact_exists(job_dir, artifact) and (
            not str(artifact).startswith(prefix) or artifact == rel
        ):
            continue
        crop["artifact"] = None
        if region is None:
            failures.append(f"table_locator_unavailable:{table['table_id']}")
            table["extraction"]["status"] = "degraded"
            _append_reason(table["extraction"], "pdf_table_crop_locator_unavailable")
            continue
        try:
            assert rel is not None
            _render_region(source, job_dir / rel, page=region[0], bbox=region[1])
        except (OSError, subprocess.SubprocessError, ValueError):
            failures.append(f"table_render_failed:{table['table_id']}")
            table["extraction"]["status"] = "degraded"
            _append_reason(table["extraction"], "pdf_table_crop_render_failed")
            continue
        crop["artifact"] = rel
        rendered += 1

    reasons = report.setdefault("reasons", [])
    operational_failures = [
        failure for failure in failures if "_source_blank:" not in failure
    ]
    source_blanks = [failure for failure in failures if "_source_blank:" in failure]
    if operational_failures:
        if "pdf_visual_render_incomplete" not in reasons:
            reasons.append("pdf_visual_render_incomplete")
    else:
        report["reasons"] = [
            reason for reason in reasons if reason != "pdf_visual_render_incomplete"
        ]
    if source_blanks and "pdf_visual_source_blank" not in report["reasons"]:
        report["reasons"].append("pdf_visual_source_blank")
    if failures and report.get("status") == "complete":
        report["status"] = "degraded"
    figures_complete = all(
        bool(figure.get("media")) and all(
            _artifact_exists(job_dir, media.get("artifact"))
            for media in figure.get("media", [])
        )
        for figure in updated.get("figures", [])
    )
    if figures_complete:
        report["reasons"] = [
            reason for reason in report["reasons"]
            if reason not in {
                "html_figure_media_incomplete", "html_visual_asset_incomplete",
            }
        ]
    if report.get("status") == "degraded" and not report["reasons"]:
        report["status"] = "complete"
    report.setdefault("metrics", {})["visual_assets_rendered"] = rendered
    report["metrics"]["visual_asset_failures"] = len(failures)
    return (
        validate_document(updated, expected_job_id=str(updated["job_id"])),
        validate_quality(report, expected_job_id=str(updated["job_id"])),
    )
