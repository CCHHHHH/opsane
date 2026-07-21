"""REST API for files attached to a chat session."""
from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
from pathlib import Path
import re
from uuid import uuid4
from weakref import WeakKeyDictionary

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

from shell_agent.attachments import (
    OFFICE_PREVIEW_EXTENSIONS,
    ParsedAttachment,
    RenderedOfficePreview,
    parse_attachment,
    render_office_pdf,
)
from shell_agent.storage.session_files import (
    claim_session_file_layout_preview,
    claim_session_file_reanalysis,
    count_session_files,
    create_session_file,
    get_session_file,
    list_session_files,
    soft_delete_session_file,
    update_session_file_layout_preview,
    update_session_file_parse,
)
from shell_agent.storage.file_transfers import has_active_file_transfer
from shell_agent.storage.sessions import ensure_session, get_session
from shell_agent.web.runtime import get_runtime


router = APIRouter()

MAX_FILE_SIZE = 512 * 1024 * 1024
MAX_FILES_PER_SESSION = 30
MAX_PREVIEW_CHARS = 250_000
INLINE_IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls", ".ppt"}
MAX_STORED_ATTACHMENT_TEXT = 400_000
OFFICE_JOB_QUEUE_TIMEOUT_SECONDS = 30
_OFFICE_JOB_LIMITERS: WeakKeyDictionary = WeakKeyDictionary()


async def _run_office_job(function, *args):
    """Bound Office work before occupying a worker; running jobs enforce their own timeout."""
    loop = asyncio.get_running_loop()
    limiter = _OFFICE_JOB_LIMITERS.get(loop)
    if limiter is None:
        limiter = asyncio.Semaphore(2)
        _OFFICE_JOB_LIMITERS[loop] = limiter
    try:
        await asyncio.wait_for(limiter.acquire(), timeout=OFFICE_JOB_QUEUE_TIMEOUT_SECONDS)
    except TimeoutError:
        raise
    try:
        return await asyncio.to_thread(function, *args)
    finally:
        limiter.release()


def _attachment_root() -> Path:
    root = Path("data/session_files")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_original_name(filename: str) -> str:
    name = os.path.basename((filename or "").strip()).replace("\x00", "")
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name).strip(" .")
    return (name or "未命名文件")[:240]


def _safe_session_segment(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:100] or "unknown"


def _stored_suffix(filename: str) -> str:
    lower = filename.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(suffix):
            return suffix
    suffix = Path(lower).suffix
    return suffix[:16] if re.fullmatch(r"\.[a-z0-9]{1,15}", suffix) else ""


def _media_type(filename: str, uploaded_type: str | None) -> str:
    supplied = (uploaded_type or "").lower().strip()
    guessed = mimetypes.guess_type(filename)[0] or ""
    if supplied and supplied != "application/octet-stream":
        return supplied
    return guessed or "application/octet-stream"


async def _save_bounded_upload(upload: UploadFile, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with destination.open("xb") as stream:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_SIZE:
                    raise HTTPException(status_code=413, detail="单个文件不能超过 512 MB")
                digest.update(chunk)
                stream.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return size, digest.hexdigest()


async def _enrich_image_content(
    rt,
    path: Path,
    original_name: str,
    media_type: str,
    parsed: ParsedAttachment,
) -> ParsedAttachment:
    """Use a configured vision model, while preserving local OCR as fallback."""
    if parsed.kind != "image" or media_type not in INLINE_IMAGE_TYPES:
        return parsed
    llm = getattr(rt, "llm", None)
    config = getattr(llm, "config", None)
    if (
        not llm
        or not config
        or not config.image_analysis_enabled
        or not str(config.api_key or "").strip()
    ):
        return parsed
    if path.stat().st_size > max(1, int(config.vision_max_bytes)):
        parsed.metadata["vision_status"] = "too_large"
        return parsed

    try:
        image_bytes = await asyncio.to_thread(path.read_bytes)
        vision_text = await llm.analyze_image_content(
            filename=original_name,
            media_type=media_type,
            image_bytes=image_bytes,
        )
    except Exception as exc:
        parsed.metadata["vision_status"] = "error"
        parsed.metadata["vision_model"] = config.vision_model or config.model
        logger.warning(f"图片视觉识别失败，继续使用本地 OCR: {type(exc).__name__}: {exc}")
        return parsed

    if not vision_text:
        parsed.metadata["vision_status"] = "empty"
        return parsed

    sections = [f"## 图片内容识别（视觉模型）\n{vision_text.strip()}"]
    if parsed.text.strip():
        sections.append(parsed.text.strip())
    combined = "\n\n".join(sections)
    if len(combined) > MAX_STORED_ATTACHMENT_TEXT:
        combined = combined[:MAX_STORED_ATTACHMENT_TEXT] + "\n... [图片识别内容已截断]"
        parsed.metadata["truncated"] = True
    parsed.text = combined
    parsed.status = "ready"
    parsed.error = ""
    parsed.metadata.update(
        {
            "analysis_supported": True,
            "vision_status": "ready",
            "vision_model": config.vision_model or config.model,
            "vision_characters": len(vision_text),
        }
    )
    return parsed


async def _parse_and_persist_session_file(rt, item: dict, path: Path) -> dict:
    """Parse one stored attachment and atomically replace its derived metadata."""
    original_name = str(item.get("original_name") or path.name)
    media_type = str(item.get("media_type") or "application/octet-stream")
    extension = _stored_suffix(original_name)
    if extension in LEGACY_OFFICE_EXTENSIONS:
        try:
            parsed = await _run_office_job(parse_attachment, path, original_name, media_type)
        except TimeoutError:
            parsed = ParsedAttachment(
                kind={".doc": "document", ".xls": "spreadsheet", ".ppt": "presentation"}[extension],
                preview_type="none",
                status="error",
                error="Office 文件解析排队超过 30 秒，请稍后重试",
            )
    else:
        parsed = await asyncio.to_thread(parse_attachment, path, original_name, media_type)
    parsed = await _enrich_image_content(rt, path, original_name, media_type, parsed)
    updated = await update_session_file_parse(
        rt.db,
        str(item.get("id") or ""),
        kind=parsed.kind,
        preview_type=parsed.preview_type,
        parse_status=parsed.status,
        parse_error=parsed.error,
        extracted_text=parsed.text,
        metadata=parsed.metadata,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="附件不存在")
    return updated


def _public_file(item: dict) -> dict:
    file_id = str(item.get("id") or "")
    return {
        "id": file_id,
        "session_id": item.get("session_id"),
        "name": item.get("original_name"),
        "media_type": item.get("media_type"),
        "extension": item.get("extension"),
        "kind": item.get("kind"),
        "preview_type": item.get("preview_type"),
        "size": item.get("size"),
        "sha256": item.get("sha256"),
        "parse_status": item.get("parse_status"),
        "parse_error": item.get("parse_error") or "",
        "metadata": item.get("metadata") or {},
        "layout_preview_status": item.get("layout_preview_status") or "none",
        "layout_preview_error": item.get("layout_preview_error") or "",
        "layout_preview_size": int(item.get("layout_preview_size") or 0),
        "created_at": item.get("created_at"),
        "preview_url": f"/api/session-files/{file_id}/preview",
        "content_url": f"/api/session-files/{file_id}/content",
        "download_url": f"/api/session-files/{file_id}/download",
    }


def _checked_path(item: dict) -> Path:
    path = Path(str(item.get("stored_path") or "")).resolve()
    root = _attachment_root()
    if root != path and root not in path.parents:
        raise HTTPException(status_code=404, detail="附件文件不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="附件文件不存在")
    return path


def _layout_preview_destination(item: dict, claim_id: str = "") -> Path:
    source = _checked_path(item)
    file_id = str(item.get("id") or "")
    filename = f"{file_id}.{claim_id}.pdf" if claim_id else f"{file_id}.pdf"
    path = (source.parent / ".previews" / filename).resolve()
    root = _attachment_root()
    if root != path and root not in path.parents:
        raise HTTPException(status_code=404, detail="附件预览路径无效")
    return path


def _checked_layout_preview_path(item: dict) -> Path:
    raw_path = str(item.get("layout_preview_path") or "")
    if not raw_path:
        raise HTTPException(status_code=404, detail="Office 版式预览尚未生成")
    path = Path(raw_path).resolve()
    source = _checked_path(item)
    preview_root = (source.parent / ".previews").resolve()
    file_id = re.escape(str(item.get("id") or ""))
    valid_name = bool(re.fullmatch(rf"{file_id}(?:\.preview_[0-9a-f]{{32}})?\.pdf", path.name))
    if path.parent != preview_root or not valid_name or path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=404, detail="Office 版式预览路径无效")
    if item.get("layout_preview_status") != "ready":
        raise HTTPException(status_code=404, detail="Office 版式预览尚未生成")
    if item.get("layout_preview_source_sha256") != item.get("sha256"):
        raise HTTPException(status_code=404, detail="Office 版式预览已过期")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Office 版式预览文件不存在")
    return path


def _layout_preview_cleanup_candidates(item: dict) -> list[Path]:
    source = _checked_path(item)
    file_id = str(item.get("id") or "")
    if not re.fullmatch(r"file_[A-Za-z0-9]+", file_id):
        return []
    preview_root = source.parent / ".previews"
    if not preview_root.is_dir():
        return []
    return [
        candidate
        for candidate in preview_root.glob(f"{file_id}*.pdf")
        if candidate.parent.resolve() == preview_root.resolve()
    ]


async def _render_and_persist_layout_preview(
    rt,
    item: dict,
    path: Path,
    claim_id: str,
) -> dict:
    destination = _layout_preview_destination(item, claim_id)
    try:
        rendered = await _run_office_job(
            render_office_pdf,
            path,
            str(item.get("original_name") or path.name),
            destination,
        )
    except TimeoutError:
        rendered = RenderedOfficePreview(
            status="error",
            error="Office 版式预览排队超过 30 秒，请稍后重试",
        )
    if rendered.status != "ready":
        destination.unlink(missing_ok=True)
    updated = await update_session_file_layout_preview(
        rt.db,
        str(item.get("id") or ""),
        status=rendered.status,
        path=str(destination) if rendered.status == "ready" else "",
        error=rendered.error,
        size=rendered.size,
        source_sha256=str(item.get("sha256") or ""),
        claim_id=claim_id,
    )
    if not updated:
        destination.unlink(missing_ok=True)
        current = await get_session_file(rt.db, str(item.get("id") or ""))
        if current:
            raise HTTPException(status_code=409, detail="该版式预览任务已被更新任务取代")
        raise HTTPException(status_code=404, detail="附件不存在")
    return updated or item


@router.get("/api/sessions/{session_id}/files")
async def api_list_session_files(session_id: str) -> dict:
    rt = get_runtime()
    if not rt.db:
        return {"files": []}
    return {"files": [_public_file(item) for item in await list_session_files(rt.db, session_id)]}


@router.post("/api/sessions/{session_id}/files")
async def api_upload_session_files(
    session_id: str,
    files: list[UploadFile] = File(...),
) -> dict:
    rt = get_runtime()
    if not rt.db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    session = await get_session(rt.db, session_id, message_limit=1)
    if not session:
        await ensure_session(rt.db, session_id, session_type="chat", title="新聊天")
    elif session.get("type") != "chat":
        raise HTTPException(status_code=400, detail="只有聊天会话可以上传附件")
    current_count = await count_session_files(rt.db, session_id)
    if current_count + len(files) > MAX_FILES_PER_SESSION:
        raise HTTPException(status_code=400, detail=f"每个会话最多保留 {MAX_FILES_PER_SESSION} 个文件")

    session_dir = _attachment_root() / _safe_session_segment(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    uploaded: list[dict] = []
    for upload in files:
        original_name = _safe_original_name(upload.filename or "")
        suffix = _stored_suffix(original_name)
        path = session_dir / f"{uuid4().hex}{suffix}"
        media_type = _media_type(original_name, upload.content_type)
        item: dict | None = None
        try:
            size, sha256 = await _save_bounded_upload(upload, path)
            if size <= 0:
                path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"文件 {original_name} 为空")
            item = await create_session_file(
                rt.db,
                session_id=session_id,
                original_name=original_name,
                stored_path=str(path),
                media_type=media_type,
                extension=suffix,
                size=size,
                sha256=sha256,
            )
            item = await _parse_and_persist_session_file(rt, item, path)
            if item:
                uploaded.append(_public_file(item))
        except HTTPException:
            if item:
                await soft_delete_session_file(rt.db, str(item.get("id") or ""))
            path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            if item:
                await soft_delete_session_file(rt.db, str(item.get("id") or ""))
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"上传 {original_name} 失败: {exc}") from exc
        finally:
            await upload.close()
    return {"files": uploaded}


@router.post("/api/session-files/{file_id}/reanalyze")
async def api_reanalyze_session_file(file_id: str) -> dict:
    """Re-run the current parser for an attachment saved by an older version."""
    rt = get_runtime()
    if not rt.db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    item = await get_session_file(rt.db, file_id)
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    extension = str(item.get("extension") or _stored_suffix(str(item.get("original_name") or ""))).lower()
    if extension not in LEGACY_OFFICE_EXTENSIONS:
        raise HTTPException(status_code=415, detail="只有旧版 DOC、XLS、PPT 文件需要重新识别")
    path = _checked_path(item)
    if not await claim_session_file_reanalysis(rt.db, file_id):
        raise HTTPException(status_code=409, detail="文件已完成解析或正在解析")
    try:
        updated = await _parse_and_persist_session_file(rt, item, path)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"重新解析附件失败: {file_id}")
        await update_session_file_parse(
            rt.db,
            file_id,
            kind=str(item.get("kind") or "other"),
            preview_type=str(item.get("preview_type") or "none"),
            parse_status="error",
            parse_error=f"重新解析失败: {type(exc).__name__}: {exc}",
            extracted_text=str(item.get("extracted_text") or ""),
            metadata=item.get("metadata") or {},
        )
        raise HTTPException(status_code=500, detail=f"重新解析失败: {exc}") from exc
    return {"file": _public_file(updated)}


@router.post("/api/session-files/{file_id}/render-preview")
async def api_render_session_file_preview(file_id: str) -> dict:
    """Generate a persistent PDF sidecar while retaining extracted text."""
    rt = get_runtime()
    if not rt.db:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    item = await get_session_file(rt.db, file_id)
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    extension = str(item.get("extension") or _stored_suffix(str(item.get("original_name") or ""))).lower()
    if extension not in OFFICE_PREVIEW_EXTENSIONS:
        raise HTTPException(status_code=415, detail="该文件不是支持版式预览的 Office 格式")
    source = _checked_path(item)
    if (
        item.get("layout_preview_status") == "ready"
        and item.get("layout_preview_source_sha256") == item.get("sha256")
    ):
        try:
            _checked_layout_preview_path(item)
            return {"file": _public_file(item)}
        except HTTPException:
            pass
    claim_id = await claim_session_file_layout_preview(rt.db, file_id)
    if not claim_id:
        current = await get_session_file(rt.db, file_id)
        if not current:
            raise HTTPException(status_code=404, detail="附件不存在")
        return JSONResponse(status_code=202, content={"file": _public_file(current)})
    try:
        updated = await _render_and_persist_layout_preview(rt, item, source, claim_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"生成 Office 版式预览失败: {file_id}")
        updated = await update_session_file_layout_preview(
            rt.db,
            file_id,
            status="error",
            error=f"生成版式预览失败: {type(exc).__name__}: {exc}",
            source_sha256=str(item.get("sha256") or ""),
            claim_id=claim_id,
        )
        if not updated:
            raise HTTPException(status_code=404, detail="附件不存在") from exc
    return {"file": _public_file(updated)}


@router.get("/api/session-files/{file_id}/content")
async def api_session_file_content(file_id: str) -> dict:
    rt = get_runtime()
    item = await get_session_file(rt.db, file_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    content = str(item.get("extracted_text") or "")
    truncated = len(content) > MAX_PREVIEW_CHARS
    if truncated:
        content = content[:MAX_PREVIEW_CHARS] + "\n\n... [预览内容已截断，请下载原文件查看完整内容]"
    return {
        "id": file_id,
        "name": item.get("original_name"),
        "content": content,
        "truncated": truncated,
        "parse_status": item.get("parse_status"),
        "parse_error": item.get("parse_error") or "",
        "metadata": item.get("metadata") or {},
    }


@router.get("/api/session-files/{file_id}/preview")
async def api_preview_session_file(file_id: str):
    rt = get_runtime()
    item = await get_session_file(rt.db, file_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    preview_type = item.get("preview_type")
    media_type = str(item.get("media_type") or "application/octet-stream")
    if preview_type not in {"pdf", "image"}:
        raise HTTPException(status_code=415, detail="该文件请使用文本预览接口")
    if preview_type == "image" and media_type not in INLINE_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="该图片格式不允许内联预览")
    if preview_type == "pdf":
        media_type = "application/pdf"
    file_path = _checked_path(item)
    filename = str(item.get("original_name") or "file")
    if preview_type == "pdf" and str(item.get("extension") or "").lower() != ".pdf":
        file_path = _checked_layout_preview_path(item)
        filename = f"{filename}.pdf"
    response = FileResponse(
        file_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/api/session-files/{file_id}/download")
async def api_download_session_file(file_id: str):
    rt = get_runtime()
    item = await get_session_file(rt.db, file_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    response = FileResponse(
        _checked_path(item),
        media_type="application/octet-stream",
        filename=str(item.get("original_name") or "file"),
        content_disposition_type="attachment",
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.delete("/api/session-files/{file_id}")
async def api_delete_session_file(file_id: str) -> dict:
    rt = get_runtime()
    if rt.db and await has_active_file_transfer(rt.db, file_id):
        raise HTTPException(status_code=409, detail="文件正在传输，暂时不能删除")
    current = await get_session_file(rt.db, file_id) if rt.db else None
    if current and (
        current.get("parse_status") == "pending"
        or current.get("layout_preview_status") == "pending"
    ):
        raise HTTPException(status_code=409, detail="文件正在解析或生成版式预览，暂时不能删除")
    item = await soft_delete_session_file(rt.db, file_id) if rt.db else None
    if not item:
        raise HTTPException(status_code=404, detail="附件不存在")
    try:
        preview_paths = _layout_preview_cleanup_candidates(item)
    except HTTPException:
        preview_paths = []
    try:
        _checked_path(item).unlink(missing_ok=True)
    except (HTTPException, OSError):
        pass
    for preview_path in preview_paths:
        try:
            preview_path.unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}
