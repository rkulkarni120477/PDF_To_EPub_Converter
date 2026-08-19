import asyncio
from html import escape
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable
from uuid import uuid4
import wave
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, StreamingResponse
import pymupdf as fitz
import cv2
import numpy as np
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("api-gateway")

app = FastAPI(title="PDF to ePub API Gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4173", "http://127.0.0.1:4173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONVERSIONS: dict[str, tuple[str, bytes]] = {}
CONVERSION_PROGRESS: dict[str, dict[str, object]] = {}
# asyncio only holds a weak reference to a task once created, so a task with no other
# referent can be garbage-collected mid-run; this set keeps every in-flight conversion
# task alive until it finishes, regardless of whether the request that started it is done.
BACKGROUND_TASKS: set[asyncio.Task] = set()


def set_progress(conversion_id: str, **fields: object) -> None:
    CONVERSION_PROGRESS[conversion_id] = {**CONVERSION_PROGRESS.get(conversion_id, {}), **fields}


def track_background_task(task: asyncio.Task) -> None:
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


# Budget for the whole Azure DI round trip. It used to be 600s, which meant an unreachable
# or wedged endpoint froze the UI for ten minutes before falling back to local extraction.
AZURE_DI_TIMEOUT_SECONDS = 150
AZURE_DI_POLLING_INTERVAL_SECONDS = 2


async def await_with_heartbeat(conversion_id: str, stage: str, awaitable, timeout: float, total_pages: int):
    """Await a long single-shot step while still publishing progress.

    Stages that are one opaque blocking call (the Azure DI analyse in particular) otherwise
    leave the status frozen on their initial value, which is indistinguishable from a hang.
    """
    task = asyncio.ensure_future(awaitable)
    started = time.monotonic()
    while True:
        done, _ = await asyncio.wait({task}, timeout=1)
        if done:
            return task.result()
        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            task.cancel()
            raise asyncio.TimeoutError
        set_progress(
            conversion_id,
            status="processing",
            stage=stage,
            current_page=0,
            total_pages=total_pages,
            detail=f"Waiting on Azure Document Intelligence - {int(elapsed)}s elapsed (times out at {int(timeout)}s)",
        )


def custom_openapi() -> dict[str, object]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version="0.1.0",
        routes=app.routes,
    )
    conversion = schema.get("paths", {}).get("/api/v1/conversions", {}).get("post", {})
    conversion.get("responses", {}).pop("422", None)
    schemas = schema.get("components", {}).get("schemas", {})
    upload_schema = schemas.get("Body_convert_pdf_api_v1_conversions_post")
    if upload_schema:
        upload_schema["required"] = ["file"]
        upload_schema["properties"]["file"] = {
            "type": "string",
            "format": "binary",
            "title": "File",
        }
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "api-gateway"}


@app.get("/_debug_azure")
async def debug_azure() -> dict[str, object]:
    import time
    source = Path(__file__).resolve().parents[2] / "test_sample.pdf"
    data = source.read_bytes()
    start = time.time()
    try:
        pages = await asyncio.wait_for(asyncio.to_thread(extract_with_azure_di, data), timeout=60)
        return {"elapsed": time.time() - start, "pages": len(pages)}
    except asyncio.TimeoutError:
        return {"elapsed": time.time() - start, "error": "timeout"}


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "service": "api-gateway",
        "capabilities": ["projects", "conversions", "downloads"],
        "azure_document_intelligence_configured": azure_di_configured(),
    }


@app.post("/api/v1/conversions")
async def convert_pdf(file: UploadFile | None = File(None), layout: str = Form("auto"), use_azure_di: bool = Form(False), narrate: bool = Form(False)) -> dict[str, str]:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="A PDF file is required.")
    if Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    source = await file.read()
    conversion_id = uuid4().hex
    set_progress(conversion_id, status="processing", stage="Starting conversion", current_page=0, total_pages=0)
    # The actual work runs as a background task instead of being awaited here, so this
    # request returns immediately - the frontend polls /status for live progress instead of
    # holding one HTTP connection open for the whole conversion (which is what made large or
    # Azure DI conversions look "stuck" and eventually time out with no EPUB produced).
    track_background_task(asyncio.create_task(run_conversion(conversion_id, source, file.filename, layout, use_azure_di, narrate)))
    return {"status": "processing", "conversion_id": conversion_id}


@app.get("/api/v1/conversions/{conversion_id}/status")
def conversion_status(conversion_id: str) -> dict[str, object]:
    progress = CONVERSION_PROGRESS.get(conversion_id)
    if progress is None:
        raise HTTPException(status_code=404, detail="Conversion not found.")
    return {**progress, "conversion_id": conversion_id}


async def run_conversion(conversion_id: str, source: bytes, filename: str, layout: str, use_azure_di: bool, narrate: bool = False) -> None:
    title = Path(filename).stem.replace("_", " ").replace("-", " ").strip() or "Converted document"
    try:
        set_progress(conversion_id, status="processing", stage="Reading PDF", current_page=0, total_pages=0)

        def extraction_progress(current_page: int, total_pages: int) -> None:
            set_progress(conversion_id, status="processing", stage="Extracting pages", current_page=current_page, total_pages=total_pages)

        try:
            pages = await asyncio.wait_for(asyncio.to_thread(extract_pdf_pages, source, extraction_progress), timeout=300)
        except (fitz.FileDataError, ValueError):
            set_progress(conversion_id, status="failed", stage="Failed", detail="The uploaded file is not a readable PDF.")
            return
        except asyncio.TimeoutError:
            set_progress(conversion_id, status="failed", stage="Failed", detail="PDF layout extraction timed out.")
            return

        # Layout is resolved once, here, so the decision is made on the locally extracted
        # text and stays stable for the rest of the run (Azure DI merging rewrites page text
        # and could otherwise flip an "auto" document to a different layout mid-conversion).
        resolved_layout = resolve_layout(layout, pages)

        # Rasterising every page at 2x into PNG is the single most expensive step, and the
        # result is only ever used as a fixed-layout background or as the canvas Azure DI
        # text is erased from. Reflowable conversions skip it entirely.
        if resolved_layout == "fixed" or use_azure_di:
            def rasterize_progress(current_page: int, total_pages: int) -> None:
                set_progress(conversion_id, status="processing", stage="Rasterizing pages", current_page=current_page, total_pages=total_pages)

            set_progress(conversion_id, status="processing", stage="Rasterizing pages", current_page=0, total_pages=len(pages))
            try:
                await asyncio.wait_for(asyncio.to_thread(render_page_images, source, pages, rasterize_progress), timeout=600)
            except asyncio.TimeoutError:
                set_progress(conversion_id, status="failed", stage="Failed", detail="Rendering page images timed out.")
                return

        if use_azure_di:
            set_progress(conversion_id, status="processing", stage="Analyzing with Azure Document Intelligence", current_page=0, total_pages=len(pages), detail="Uploading document to Azure Document Intelligence")
            try:
                azure_pages = await await_with_heartbeat(
                    conversion_id,
                    "Analyzing with Azure Document Intelligence",
                    asyncio.to_thread(extract_with_azure_di, source),
                    AZURE_DI_TIMEOUT_SECONDS,
                    len(pages),
                )
            except AzureExtractionError as error:
                set_progress(conversion_id, status="failed", stage="Failed", detail=str(error))
                return
            except asyncio.TimeoutError:
                # Azure DI only enriches text and cleans the page backgrounds, so a slow or
                # unreachable endpoint degrades to the local extraction instead of failing.
                logger.warning("Azure Document Intelligence extraction timed out after %ss; continuing with local PDF extraction.", AZURE_DI_TIMEOUT_SECONDS)
                set_progress(conversion_id, status="processing", stage="Azure DI timed out - using local extraction", current_page=0, total_pages=len(pages), detail=None)
                use_azure_di = False

        if use_azure_di:
            def merge_progress(current_page: int, total_pages: int) -> None:
                set_progress(conversion_id, status="processing", stage="Cleaning page images", current_page=current_page, total_pages=total_pages, detail=None)

            set_progress(conversion_id, status="processing", stage="Cleaning page images", current_page=0, total_pages=len(pages), detail=None)
            try:
                pages = await asyncio.wait_for(asyncio.to_thread(merge_azure_text, pages, azure_pages, merge_progress), timeout=300)
            except asyncio.TimeoutError:
                logger.warning("Azure Document Intelligence merge timed out; continuing with local PDF extraction.")
                use_azure_di = False

        def build_progress(current_page: int, total_pages: int) -> None:
            set_progress(conversion_id, status="processing", stage="Rendering pages", current_page=current_page, total_pages=total_pages)

        set_progress(conversion_id, status="processing", stage="Rendering pages", current_page=0, total_pages=len(pages))
        try:
            epub = await asyncio.wait_for(asyncio.to_thread(build_epub, title, pages, resolved_layout, build_progress, narrate), timeout=1800)
        except RuntimeError as error:
            set_progress(conversion_id, status="failed", stage="Failed", detail=str(error))
            return
        except asyncio.TimeoutError:
            set_progress(conversion_id, status="failed", stage="Failed", detail="EPUB generation timed out. Try a smaller PDF or fewer pages.")
            return

        set_progress(conversion_id, status="processing", stage="Packaging EPUB", current_page=len(pages), total_pages=len(pages))
        CONVERSIONS[conversion_id] = (f"{title}.epub", epub)
        set_progress(
            conversion_id,
            status="completed",
            stage="Completed",
            current_page=len(pages),
            total_pages=len(pages),
            title=title,
            pages=len(pages),
            layout=resolved_layout,
            azure_document_intelligence=use_azure_di,
            download_url=f"/api/v1/downloads/{conversion_id}",
        )
    except Exception:
        logger.exception("Conversion %s failed unexpectedly", conversion_id)
        set_progress(conversion_id, status="failed", stage="Failed", detail="An unexpected error occurred during conversion.")


class AzureExtractionError(RuntimeError):
    pass


def azure_di_configured() -> bool:
    return bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") and os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY"))


def extract_with_azure_di(source: bytes) -> list[dict[str, object]]:
    endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    if not endpoint or not key:
        raise AzureExtractionError("Azure Document Intelligence is enabled, but AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY are not configured for the gateway process.")
    try:
        client = DocumentIntelligenceClient(
            endpoint=endpoint.rstrip("/"),
            credential=AzureKeyCredential(key),
            # Without explicit socket timeouts a wedged endpoint leaves the request blocked
            # indefinitely, which reads as a hung conversion rather than a failed one.
            connection_timeout=30,
            read_timeout=60,
        )
        poller = client.begin_analyze_document(
            "prebuilt-layout",
            body=BytesIO(source),
            content_type="application/pdf",
            polling_interval=AZURE_DI_POLLING_INTERVAL_SECONDS,
        )
        result = poller.result()
    except HttpResponseError as error:
        status_code = getattr(error, "status_code", None)
        reason = getattr(getattr(error, "error", None), "message", None) or error.message or str(error)
        logger.error("Azure DI HttpResponseError (status=%s): %s", status_code, reason)
        if status_code in (401, 403):
            raise AzureExtractionError("Azure Document Intelligence rejected the credentials or endpoint (HTTP authentication error).") from error
        if status_code == 400:
            raise AzureExtractionError(f"Azure Document Intelligence rejected the PDF as invalid or unsupported (HTTP 400): {reason}") from error
        raise AzureExtractionError(f"Azure Document Intelligence returned an error{f' (HTTP {status_code})' if status_code else ''}: {reason}") from error
    except ServiceRequestError as error:
        logger.error("Azure DI ServiceRequestError: %s", error)
        raise AzureExtractionError("Azure Document Intelligence could not be reached. Check the endpoint and network connection.") from error
    except ServiceResponseError as error:
        logger.error("Azure DI ServiceResponseError: %s", error)
        raise AzureExtractionError("Azure Document Intelligence connection was interrupted while waiting for a response. This can happen with large files; try again or use a smaller PDF.") from error
    except Exception as error:
        logger.exception("Azure DI extraction failed with an unexpected error")
        raise AzureExtractionError(f"Azure Document Intelligence could not extract this PDF ({type(error).__name__}: {error}).") from error
    pages: list[dict[str, object]] = []
    for page in result.pages or []:
        lines = [
            {"text": line.content, "spans": [], "polygon": list(line.polygon or [])}
            for line in (page.lines or []) if line.content.strip()
        ]
        words = [
            {"text": word.content, "polygon": list(word.polygon or [])}
            for word in (page.words or []) if word.content.strip()
        ]
        pages.append({
            "text": "\n".join(line["text"] for line in lines),
            "lines": lines,
            "words": words,
            "width": page.width,
            "height": page.height,
        })
    return pages


def merge_azure_text(pages: list[dict[str, object]], azure_pages: list[dict[str, object]], on_progress: Callable[[int, int], None] | None = None) -> list[dict[str, object]]:
    for index, azure_page in enumerate(azure_pages):
        if on_progress:
            on_progress(index + 1, len(azure_pages))
        if index >= len(pages) or not azure_page["text"]:
            continue
        pages[index]["text"] = azure_page["text"]
        pages[index]["azure_lines"] = azure_page["lines"]
        pages[index]["background_image_text"] = azure_page["text"]
        # dict.get(key, default) only falls back when the key is absent - Azure Document
        # Intelligence's width/height are optional and can come back None for a page, in
        # which case .get() would still return that None and silently defeat every erasure
        # below (remove_ocr_text_from_background bails out on a falsy ocr_width/ocr_height).
        # `or` falls back on None (and on 0) too, so text actually gets erased either way.
        azure_width = azure_page.get("width") or pages[index]["width"]
        azure_height = azure_page.get("height") or pages[index]["height"]
        pages[index]["azure_width"] = azure_width
        pages[index]["azure_height"] = azure_height
        ocr_regions = azure_page.get("words", []) or azure_page["lines"]
        pages[index]["page_image"] = remove_ocr_text_from_background(
            pages[index]["page_image"], ocr_regions, azure_width, azure_height
        )
        remove_ocr_text_from_embedded_images(
            pages[index]["blocks"], ocr_regions, pages[index]["width"], pages[index]["height"], azure_width, azure_height
        )
    return pages


def remove_ocr_text_from_background(
    image_bytes: bytes,
    lines: list[dict[str, object]],
    ocr_width: float,
    ocr_height: float,
) -> bytes:
    image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or not ocr_width or not ocr_height:
        return image_bytes
    mask = np.zeros(image.shape[:2], dtype=np.uint8)
    image_height, image_width = image.shape[:2]
    for line in lines:
        polygon = line.get("polygon", [])
        points = [
            (
                round(float(polygon[i]) / ocr_width * image_width),
                round(float(polygon[i + 1]) / ocr_height * image_height),
            )
            for i in range(0, len(polygon) - 1, 2)
        ]
        if len(points) >= 3:
            cv2.fillPoly(mask, [np.array(points, dtype=np.int32)], 255)
    if not np.any(mask):
        return image_bytes
    scale = min(image_width / ocr_width, image_height / ocr_height)
    padding = max(2, round(scale * 1.5))
    kernel_size = padding * 2 + 1
    mask = cv2.dilate(mask, np.ones((kernel_size, kernel_size), dtype=np.uint8), iterations=1)
    cleaned = cv2.inpaint(image, mask, max(3, padding), cv2.INPAINT_TELEA)
    success, encoded = cv2.imencode(".png", cleaned)
    return encoded.tobytes() if success else image_bytes


def remove_ocr_text_from_embedded_images(
    blocks: list[dict[str, object]],
    ocr_regions: list[dict[str, object]],
    page_width: float,
    page_height: float,
    azure_width: float,
    azure_height: float,
) -> None:
    if not ocr_regions or not azure_width or not azure_height:
        return
    for block in blocks:
        if block.get("type") != "image":
            continue
        left, top, right, bottom = block["bbox"]
        block_width = right - left
        block_height = bottom - top
        if block_width <= 0 or block_height <= 0:
            continue
        local_regions: list[dict[str, object]] = []
        for region in ocr_regions:
            polygon = region.get("polygon", [])
            if len(polygon) < 6:
                continue
            xs = [float(polygon[i]) / azure_width * page_width for i in range(0, len(polygon) - 1, 2)]
            ys = [float(polygon[i + 1]) / azure_height * page_height for i in range(0, len(polygon) - 1, 2)]
            center_x, center_y = sum(xs) / len(xs), sum(ys) / len(ys)
            if not (left <= center_x <= right and top <= center_y <= bottom):
                continue
            local_regions.append({"polygon": [value for x, y in zip(xs, ys) for value in (x - left, y - top)]})
        if not local_regions:
            continue
        cleaned = remove_ocr_text_from_background(block["data"], local_regions, block_width, block_height)
        if cleaned != block["data"]:
            block["data"] = cleaned
            block["ext"] = "png"


@app.get("/api/v1/downloads/{conversion_id}")
def download_conversion(conversion_id: str) -> StreamingResponse:
    if conversion_id not in CONVERSIONS:
        raise HTTPException(status_code=404, detail="Conversion not found.")
    filename, content = CONVERSIONS[conversion_id]
    return StreamingResponse(
        BytesIO(content),
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/v1/previews/{conversion_id}", response_class=HTMLResponse)
def preview_conversion(conversion_id: str) -> HTMLResponse:
    if conversion_id not in CONVERSIONS:
        raise HTTPException(status_code=404, detail="Conversion not found.")
    _, epub_bytes = CONVERSIONS[conversion_id]
    with ZipFile(BytesIO(epub_bytes)) as archive:
        names = set(archive.namelist())
        style = archive.read("OEBPS/style.css").decode("utf-8", errors="replace") if "OEBPS/style.css" in names else ""
        page_names = sorted(name for name in names if re.fullmatch(r"OEBPS/page-\d+\.xhtml", name))
        pages = []
        for page_name in page_names:
            page_html = archive.read(page_name).decode("utf-8", errors="replace")
            body_match = re.search(r"<body[^>]*>(.*?)</body>", page_html, re.IGNORECASE | re.DOTALL)
            body = body_match.group(1) if body_match else page_html
            body = inline_epub_assets(body, archive, names)
            pages.append(body)
    if not pages:
        raise HTTPException(status_code=422, detail="The EPUB contains no readable pages.")
    page_markup = "".join(f'<article class="reader-page" data-page="{index}">{body}</article>' for index, body in enumerate(pages, start=1))
    reader_html = f'''<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>Academian ePub Reader</title><style>{style} body {{ margin:0; background:#142c2b; color:#17232b; font-family:Georgia,serif; }} .reader-shell {{ min-height:100vh; }} .reader-bar {{ position:sticky; top:0; z-index:20; display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 22px; color:#eaf7f2; background:#123c3a; font:600 13px system-ui,sans-serif; }} .reader-controls {{ display:flex; align-items:center; gap:10px; }} .reader-controls button {{ border:0; padding:0; color:#eaf7f2; background:transparent; cursor:pointer; text-decoration:underline; }} .reader-controls button:hover {{ color:#fff; }} .reader-count {{ color:#a7cbc0; font-size:11px; }} .reader-pages {{ display:grid; gap:24px; padding:28px 18px 50px; }} .reader-page {{ display:none; position:relative; margin:0 auto; max-width:100%; overflow:hidden; background:white; box-shadow:0 8px 28px #08181766; }} .reader-page.active {{ display:block; }} .reader-page .pdf-page-background {{ max-width:100%; }} .reader-page audio,.reader-page video {{ max-width:100%; }} .reader-page.reflowable {{ min-height:75vh; padding:42px clamp(22px,7vw,96px); box-sizing:border-box; }} .reader-page.reflowable .page-text {{ position:static; }} .reader-page.reflowable .pdf-media {{ position:static !important; display:block; margin:18px 0; }} .reader-page.reflowable .pdf-widget {{ position:static; display:block; margin:10px 0; }} .reader-page.fixed .pdf-page-background {{ display:block; width:100%; height:100%; }} .reader-page.fixed .accessibility-text {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); }} </style></head><body><div class="reader-shell"><header class="reader-bar"><strong>Academian ePub Reader</strong><div class="reader-controls"><button id="previous" type="button">Previous</button><span class="reader-count" id="readerCount">Page 1 of {len(pages)}</span><button id="next" type="button">Next</button><button id="exitReader" type="button">Exit reader</button></div></header><main class="reader-pages">{page_markup}</main></div><script>const pages=[...document.querySelectorAll('.reader-page')];let current=0;const count=document.querySelector('#readerCount');function show(index){{current=Math.max(0,Math.min(index,pages.length-1));pages.forEach((page,i)=>page.classList.toggle('active',i===current));count.textContent=`Page ${{current+1}} of ${{pages.length}}`;window.scrollTo({{top:0,behavior:'smooth'}});}}document.querySelector('#previous').onclick=()=>show(current-1);document.querySelector('#next').onclick=()=>show(current+1);document.querySelector('#exitReader').onclick=()=>{{if(window.history.length>1){{window.history.back();}}else{{window.close();}}}};show(0);</script></body></html>'''
    return HTMLResponse(reader_html)


def inline_epub_assets(markup: str, archive: ZipFile, names: set[str]) -> str:
    def replace_source(match: re.Match[str]) -> str:
        prefix, source, suffix = match.group(1), match.group(2), match.group(3)
        asset_name = f"OEBPS/{source.lstrip('./')}"
        if asset_name not in names:
            return match.group(0)
        extension = Path(source).suffix.lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "svg": "image/svg+xml", "mp3": "audio/mpeg", "wav": "audio/wav", "mp4": "video/mp4", "webm": "video/webm"}.get(extension, "application/octet-stream")
        import base64
        data_uri = f"data:{mime};base64,{base64.b64encode(archive.read(asset_name)).decode('ascii')}"
        return f"{prefix}{data_uri}{suffix}"
    return re.sub(r'((?:src|href)=["\'])([^"\']+)(["\'])', replace_source, markup)


@app.delete("/api/v1/conversions")
def clear_conversions() -> dict[str, int | str]:
    deleted_count = len(CONVERSIONS)
    CONVERSIONS.clear()
    CONVERSION_PROGRESS.clear()
    return {"status": "cleared", "deleted": deleted_count}


def extract_pdf_pages(source: bytes, on_progress: Callable[[int, int], None] | None = None) -> list[dict[str, object]]:
    document = fitz.open(stream=source, filetype="pdf")
    pages: list[dict[str, object]] = []
    document_media = extract_document_media(document)
    total_pages = len(document)
    try:
        for page_number, page in enumerate(document, start=1):
            if on_progress:
                on_progress(page_number, total_pages)
            page_dict = page.get_text("dict")
            complete_text = page.get_text("text", sort=True).strip()
            blocks: list[dict[str, object]] = []
            page_text: list[str] = []
            for block in page_dict.get("blocks", []):
                if block.get("type") == 1 and block.get("image"):
                    blocks.append({"type": "image", "bbox": block["bbox"], "data": block["image"], "ext": block.get("ext", "png")})
                    continue
                if block.get("type") != 0:
                    continue
                lines: list[dict[str, object]] = []
                for line in block.get("lines", []):
                    line_spans: list[dict[str, object]] = []
                    line_text: list[str] = []
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if not text:
                            continue
                        line_spans.append({
                            "text": text,
                            "bbox": span["bbox"],
                            "font": span.get("font", "sans-serif"),
                            "size": span.get("size", 12),
                            "color": span.get("color", 0),
                            "flags": span.get("flags", 0),
                        })
                        line_text.append(text)
                    if line_spans:
                        lines.append({"bbox": line["bbox"], "spans": line_spans})
                        page_text.append("".join(line_text))
                if lines:
                    blocks.append({"type": "text", "bbox": block["bbox"], "lines": lines})
            for widget in page.widgets() or []:
                field_type = {
                    getattr(fitz, "PDF_WIDGET_TYPE_TEXT", 7): "text",
                    getattr(fitz, "PDF_WIDGET_TYPE_CHECKBOX", 2): "checkbox",
                    getattr(fitz, "PDF_WIDGET_TYPE_RADIOBUTTON", 5): "radio",
                    getattr(fitz, "PDF_WIDGET_TYPE_COMBOBOX", 3): "select",
                    getattr(fitz, "PDF_WIDGET_TYPE_LISTBOX", 4): "select",
                }.get(widget.field_type, "text")
                choices = list(widget.choice_values or [])
                blocks.append({
                    "type": "widget",
                    "bbox": tuple(widget.rect),
                    "field_type": field_type,
                    "name": widget.field_name or f"field-{len(blocks) + 1}",
                    "value": widget.field_value or "",
                    "choices": choices,
                    "checked": bool(widget.field_value and widget.field_value not in ("Off", "0")),
                })
            media = list(document_media if len(pages) == 0 else [])
            media.extend(extract_page_media(page))
            pages.append({
                # cropbox, not page.rect: block/line/span/widget/annotation bboxes below are all
                # in this unrotated coordinate space, and transform_bbox() expects unrotated
                # width/height to rotate them into the visual layout. page.rect is already
                # rotation-corrected, so feeding it in here would rotate everything twice and
                # scatter text/images/widgets off their real positions on any rotated page.
                "width": page.cropbox.width,
                "height": page.cropbox.height,
                "rotation": page.rotation,
                "blocks": blocks,
                "text": complete_text or " ".join(page_text).strip(),
                # Filled in later by render_page_images(), and only when a fixed layout or an
                # Azure DI pass actually needs the rasterised page.
                "page_image": b"",
                "media": media,
            })
    finally:
        document.close()
    return pages


def render_page_images(source: bytes, pages: list[dict[str, object]], on_progress: Callable[[int, int], None] | None = None) -> None:
    document = fitz.open(stream=source, filetype="pdf")
    total_pages = len(pages)
    try:
        for index, page in enumerate(document, start=1):
            if index > total_pages:
                break
            if on_progress:
                on_progress(index, total_pages)
            pages[index - 1]["page_image"] = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
    finally:
        document.close()


def extract_document_media(document: fitz.Document) -> list[dict[str, object]]:
    media: list[dict[str, object]] = []
    for name in document.embfile_names():
        embedded = document.embfile_get(name)
        data = embedded.get("file", b"") if isinstance(embedded, dict) else b""
        if data and media_mime_type(name):
            media.append({"name": name, "data": data, "mime": media_mime_type(name), "kind": media_kind(name)})
    return media


def extract_page_media(page: fitz.Page) -> list[dict[str, object]]:
    media: list[dict[str, object]] = []
    for annot in page.annots() or []:
        annot_type = annot.type[0] if annot.type else 0
        if annot_type == getattr(fitz, "PDF_ANNOT_FILE_ATTACHMENT", 17):
            info = annot.file_info or {}
            name = info.get("filename", f"attachment-{annot.xref}.bin")
            data = annot.get_file().get("file", b"")
        elif annot_type in (getattr(fitz, "PDF_ANNOT_SOUND", 18), getattr(fitz, "PDF_ANNOT_MOVIE", 19), getattr(fitz, "PDF_ANNOT_RICH_MEDIA", 20)):
            info = annot.file_info or {}
            name = info.get("filename", f"media-{annot.xref}.bin")
            getter = annot.get_sound if annot_type == getattr(fitz, "PDF_ANNOT_SOUND", 18) else annot.get_file
            result = getter()
            data = result.get("file", b"") if isinstance(result, dict) else b""
        else:
            continue
        mime = media_mime_type(name)
        if data and mime:
            media.append({"name": name, "data": data, "mime": mime, "kind": media_kind(name), "bbox": tuple(annot.rect)})
    return media


def media_mime_type(name: str) -> str | None:
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac", "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "avi": "video/x-msvideo"}.get(Path(name).suffix.lower().lstrip("."))


def media_kind(name: str) -> str:
    return "audio" if media_mime_type(name).startswith("audio/") else "video"


def resolve_layout(layout: str, pages: list[dict[str, object]]) -> str:
    if layout in ("fixed", "reflowable"):
        return layout
    text_pages = sum(bool(str(page.get("text", "")).strip()) for page in pages)
    return "reflowable" if text_pages >= max(1, len(pages) // 2) else "fixed"


def build_epub(title: str, pages: list[dict[str, object]], layout: str = "auto", on_progress: Callable[[int, int], None] | None = None, narrate: bool = False) -> bytes:
    resolved_layout = resolve_layout(layout, pages)
    fixed_layout = resolved_layout == "fixed"
    narration_deadline = time.monotonic() + 120
    page_documents: list[tuple[str, str]] = []
    overlay_documents: list[tuple[str, str]] = []
    audio_files: list[tuple[str, bytes]] = []
    image_files: list[tuple[str, bytes, str]] = []
    media_files: list[tuple[str, bytes, str]] = []
    for index, page in enumerate(pages, start=1):
        if on_progress:
            on_progress(index, len(pages))
        raw_text = str(page["text"]).strip()
        page_text = raw_text or "This page did not contain extractable text."
        width = float(page["width"])
        height = float(page["height"])
        rotation = int(page["rotation"])
        page_width, page_height = (height, width) if rotation in (90, 270) else (width, height)
        if fixed_layout:
            markup, page_images = render_page(page, index, width, height, rotation, include_text=False)
            markup = f'{render_fixed_accessibility_layer(page, index, page_width, page_height)}{markup}'
        else:
            markup, page_images = render_reflow_page(page, index)
        media_markup, page_media_files = render_media(page.get("media", []), index, width, height, rotation, fixed_layout)
        background_name = f"OEBPS/images/page-{index}-background.png"
        if fixed_layout:
            image_files.append((background_name, page["page_image"], "png"))
            markup = f'<img class="pdf-page-background" src="images/page-{index}-background.png" alt="" aria-hidden="true"/>{markup}{media_markup}'
        else:
            markup = f'{markup}{media_markup}'
        page_name = f"page-{index}.xhtml"
        page_documents.append(
            (page_name, f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{escape(title)} - Page {index}</title><meta charset="utf-8"/><meta name="viewport" content="width={page_width:.0f}, height={page_height:.0f}"/><link rel="stylesheet" type="text/css" href="style.css"/></head><body><section id="page-{index}" class="page {resolved_layout}" style="{f"width:{page_width:.2f}px;height:{page_height:.2f}px;" if fixed_layout else ""}" data-rotation="{rotation}"><div id="page-{index}-content" class="page-text">{markup}</div></section></body></html>''')
        )
        image_files.extend((name, data, Path(name).suffix.lstrip(".")) for name, data in page_images)
        media_files.extend(page_media_files)
        narration = None
        # Each narrated page costs a fresh Python + SAPI subprocess, which dominated the
        # runtime of every conversion, so Read Aloud audio is only produced on request.
        if narrate and raw_text and time.monotonic() < narration_deadline:
            try:
                narration = synthesize_speech(page_text, max_seconds=narration_deadline - time.monotonic())
            except RuntimeError as error:
                # Narration is a nice-to-have on top of the extracted page content; a stuck
                # or failing TTS pass must not abort the whole conversion, so this page is
                # emitted without audio instead of propagating the error.
                logger.warning("Skipping narration for page %d: %s", index, error)
        if narration is not None:
            audio_bytes, duration = narration
            audio_name = f"OEBPS/audio/page-{index}.wav"
            audio_files.append((audio_name, audio_bytes))
            overlay_documents.append(
                (f"overlay-{index}.smil", f'''<?xml version="1.0" encoding="UTF-8"?><smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body><par id="page-{index}-par"><text src="{page_name}#page-{index}-text"/><audio src="audio/page-{index}.wav" clipBegin="0.0" clipEnd="{duration:.3f}"/></par></body></smil>''')
            )
        else:
            # No extractable text on this page (image-only/blank), or narration failed/timed
            # out - skip the audio overlay rather than narrating a placeholder sentence.
            overlay_documents.append(
                (f"overlay-{index}.smil", f'''<?xml version="1.0" encoding="UTF-8"?><smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body><par id="page-{index}-par"><text src="{page_name}#page-{index}-text"/></par></body></smil>''')
            )
    safe_title = escape(title)
    container = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    page_manifest = "".join(f'<item id="page-{index}" href="{name}" media-type="application/xhtml+xml"/>' for index, (name, _) in enumerate(page_documents, start=1))
    overlay_manifest = "".join(f'<item id="overlay-{index}" href="{name}" media-type="application/smil+xml"/>' for index, (name, _) in enumerate(overlay_documents, start=1))
    audio_manifest = "".join(f'<item id="audio-{index}" href="{name.removeprefix("OEBPS/")}" media-type="audio/wav"/>' for index, (name, _) in enumerate(audio_files, start=1))
    image_manifest = "".join(f'<item id="image-{index}" href="{name.removeprefix("OEBPS/")}" media-type="image/{extension}"/>' for index, (name, _, extension) in enumerate(image_files, start=1))
    media_manifest = "".join(f'<item id="media-{index}" href="{name.removeprefix("OEBPS/")}" media-type="{mime}"/>' for index, (name, _, mime) in enumerate(media_files, start=1))
    spine = "".join(f'<itemref idref="page-{index}" media-overlay="overlay-{index}"/>' for index in range(1, len(page_documents) + 1))
    opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">urn:uuid:{uuid4()}</dc:identifier><dc:title>{safe_title}</dc:title><dc:language>en</dc:language><meta property="rendition:layout">{"pre-paginated" if fixed_layout else "reflowable"}</meta><meta property="rendition:orientation">auto</meta><meta property="rendition:spread">none</meta><meta property="media:active-class">-epub-media-overlay-active</meta></metadata>
    <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="style" href="style.css" media-type="text/css"/>{page_manifest}{overlay_manifest}{audio_manifest}{image_manifest}{media_manifest}</manifest>
    <spine>{spine}</spine>
</package>'''
    nav_items = "".join(f'<li><a href="{name}">Page {index}</a></li>' for index, (name, _) in enumerate(page_documents, start=1))
    nav = f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{safe_title}</title></head><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Contents</h1><ol>{nav_items}</ol></nav></body></html>'''
    style = ".-epub-media-overlay-active { background: #fff2a8 !important; } .page { margin: 0 auto; } .page.fixed { position: relative; overflow: hidden; page-break-after: always; } .page.reflowable { max-width: 48rem; padding: 2rem 1.5rem; } .fixed .page-text { position: absolute; inset: 0; z-index: 2; } .fixed .accessibility-text { width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: normal; border: 0; } .fixed .accessibility-text p { margin: 0; } .fixed .ocr-text-layer { width: auto; height: auto; margin: 0; overflow: visible; clip: auto; white-space: normal; color: #17232b; } .fixed .ocr-line { position: absolute; white-space: nowrap; overflow: hidden; } .pdf-page-background { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: fill; z-index: 1; } .pdf-span { display: inline; vertical-align: baseline; } .reflow-image { display: block; max-width: 100%; height: auto; margin: 1rem auto; } .reflow-paragraph { margin: 0 0 .75rem; line-height: 1.45; } .reflow-field { display: block; margin: .75rem 0; color: #17232b; } .reflow-field input, .reflow-field select { margin-left: .5rem; padding: .35rem; } .pdf-widget { position: absolute; z-index: 4; box-sizing: border-box; font: inherit; color: #111; background: rgba(255,255,255,.88); border: 1px solid #4d6670; padding: 2px 4px; } .pdf-checkbox, .pdf-radio { padding: 0; accent-color: #0c7770; background: rgba(255,255,255,.95); } .pdf-select { padding: 0 2px; } .pdf-media { z-index: 5; background: rgba(255,255,255,.95); }"
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        archive.writestr("META-INF/container.xml", container, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/content.opf", opf, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/nav.xhtml", nav, compress_type=ZIP_DEFLATED)
        archive.writestr("OEBPS/style.css", style, compress_type=ZIP_DEFLATED)
        for page_name, page_content in page_documents:
            archive.writestr(f"OEBPS/{page_name}", page_content, compress_type=ZIP_DEFLATED)
        for overlay_name, overlay_content in overlay_documents:
            archive.writestr(f"OEBPS/{overlay_name}", overlay_content, compress_type=ZIP_DEFLATED)
        for audio_name, audio_bytes in audio_files:
            archive.writestr(audio_name, audio_bytes, compress_type=ZIP_DEFLATED)
        for image_name, image_bytes, _ in image_files:
            archive.writestr(image_name, image_bytes, compress_type=ZIP_DEFLATED)
        for media_name, media_bytes, _ in media_files:
            archive.writestr(media_name, media_bytes, compress_type=ZIP_DEFLATED)
    return output.getvalue()


def render_reflow_page(page: dict[str, object], index: int) -> tuple[str, list[tuple[str, bytes]]]:
    markup: list[str] = []
    images: list[tuple[str, bytes]] = []
    azure_lines = page.get("azure_lines")
    if azure_lines:
        for line_index, line in enumerate(azure_lines):
            markup.append(f'<p class="reflow-paragraph" id="page-{index}-line-{line_index}">{escape(str(line["text"]))}</p>')
    elif str(page.get("text", "")).strip():
        for line in str(page["text"]).splitlines():
            if line.strip():
                markup.append(f'<p class="reflow-paragraph">{escape(line)}</p>')
    for block_index, block in enumerate(page["blocks"]):
        if block["type"] == "image":
            image_name = f"OEBPS/images/page-{index}-{block_index}.{block['ext']}"
            images.append((image_name, block["data"]))
            markup.append(f'<img class="reflow-image" src="images/page-{index}-{block_index}.{block["ext"]}" alt="Page {index} image"/>')
        elif block["type"] == "widget":
            markup.append(render_reflow_widget(block))
        elif block["type"] == "text":
            continue
    if not markup:
        markup.append('<p class="reflow-paragraph">No extractable text on this page.</p>')
    return "".join(markup), images


def render_fixed_accessibility_layer(page: dict[str, object], index: int, page_width: float, page_height: float) -> str:
    azure_lines = page.get("azure_lines")
    if azure_lines:
        # Azure Document Intelligence reports polygons against the page as it visually appears
        # (already rotated), the same orientation as the background image and its page_width x
        # page_height container - so lines are scaled straight into that space, not the PDF's
        # unrotated width/height (which would leave lines scattered on any rotated page).
        azure_width = float(page.get("azure_width", page_width))
        azure_height = float(page.get("azure_height", page_height))
        boxes: list[dict[str, float | str]] = []
        for line in azure_lines:
            polygon = line.get("polygon", [])
            if len(polygon) < 8:
                continue
            xs = [float(polygon[i]) for i in range(0, len(polygon), 2)]
            ys = [float(polygon[i]) for i in range(1, len(polygon), 2)]
            boxes.append({
                "text": str(line["text"]),
                "left": min(xs) / azure_width * page_width,
                "top": min(ys) / azure_height * page_height,
                "right": max(xs) / azure_width * page_width,
                "bottom": max(ys) / azure_height * page_height,
            })
        # Clamp each line's bottom edge to the next line's top so overlapping/oversized
        # polygons can never make adjacent OCR text lines visually overlap.
        boxes.sort(key=lambda box: box["top"])
        for box_index, box in enumerate(boxes):
            later_tops = [other["top"] for other in boxes[box_index + 1:] if other["top"] > box["top"] + 1]
            if later_tops:
                box["bottom"] = min(box["bottom"], min(later_tops))
        line_markup = []
        for line_index, box in enumerate(boxes):
            line_width = max(box["right"] - box["left"], 1)
            line_height = max(box["bottom"] - box["top"], 1)
            font_size = max(min(line_height * 0.82, 96), 6)
            line_markup.append(
                f'<div class="ocr-line" id="page-{index}-line-{line_index}" '
                f'style="left:{box["left"]:.2f}px;top:{box["top"]:.2f}px;width:{line_width:.2f}px;height:{line_height:.2f}px;'
                f'font-size:{font_size:.2f}px;line-height:{line_height:.2f}px;">{escape(str(box["text"]))}</div>'
            )
        if line_markup:
            return f'<div id="page-{index}-text" class="page-text ocr-text-layer" aria-label="OCR text for page {index}">{"".join(line_markup)}</div>'
        content = "".join(f'<p>{escape(str(line["text"]))}</p>' for line in azure_lines if str(line["text"]).strip())
        return f'<div id="page-{index}-text" class="page-text accessibility-text" aria-label="OCR text for page {index}">{content}</div>'
    paragraphs = [
        f"<p>{escape(line)}</p>"
        for line in str(page.get("text", "")).splitlines()
        if line.strip()
    ]
    content = "".join(paragraphs) or "<p>No extractable text on this page.</p>"
    return f'<div id="page-{index}-text" class="page-text accessibility-text" aria-label="Extracted text for page {index}">{content}</div>'


def render_reflow_widget(widget: dict[str, object]) -> str:
    name = escape(str(widget["name"]), quote=True)
    value = escape(str(widget["value"]), quote=True)
    if widget["field_type"] == "checkbox":
        checked = " checked" if widget["checked"] else ""
        return f'<label class="reflow-field"><input type="checkbox" name="{name}" value="{value or "on"}"{checked}/> {name}</label>'
    if widget["field_type"] == "select":
        options = "".join(f'<option value="{escape(str(choice), quote=True)}"{" selected" if str(choice) == str(widget["value"]) else ""}>{escape(str(choice))}</option>' for choice in widget["choices"])
        return f'<label class="reflow-field">{name}<select name="{name}">{options}</select></label>'
    return f'<label class="reflow-field">{name}<input type="text" name="{name}" value="{value}"/></label>'


def render_media(media: list[dict[str, object]], index: int, width: float, height: float, rotation: int, fixed_layout: bool) -> tuple[str, list[tuple[str, bytes, str]]]:
    markup: list[str] = []
    files: list[tuple[str, bytes, str]] = []
    for media_index, item in enumerate(media, start=1):
        original_name = Path(str(item["name"])).name
        safe_name = "".join(char if char.isalnum() or char in ".-_" else "_" for char in original_name)
        asset_name = f"OEBPS/media/page-{index}-{media_index}-{safe_name}"
        files.append((asset_name, item["data"], item["mime"]))
        bbox = item.get("bbox")
        if bbox and fixed_layout:
            left, top, right, bottom = transform_bbox(bbox, width, height, rotation)
            style = f"left:{left:.2f}px;top:{top:.2f}px;width:{max(right-left, 160):.2f}px;height:{max(bottom-top, 80):.2f}px;"
        elif fixed_layout:
            style = "left:20px;bottom:20px;width:280px;height:auto;"
        else:
            style = "display:block;width:min(100%, 640px);margin:1rem 0;"
        relative_name = asset_name.removeprefix("OEBPS/")
        if item["kind"] == "audio":
            markup.append(f'<audio class="pdf-media" controls preload="metadata" style="{style}" aria-label="Audio from PDF"><source src="{relative_name}" type="{item["mime"]}"/></audio>')
        else:
            markup.append(f'<video class="pdf-media" controls preload="metadata" style="{style}" aria-label="Video from PDF"><source src="{relative_name}" type="{item["mime"]}"/></video>')
    return "".join(markup), files


def render_page(page: dict[str, object], index: int, width: float, height: float, rotation: int, include_text: bool = True) -> tuple[str, list[tuple[str, bytes]]]:
    markup: list[str] = []
    images: list[tuple[str, bytes]] = []
    for block_index, block in enumerate(page["blocks"]):
        bbox = block["bbox"]
        left, top, right, bottom = transform_bbox(bbox, width, height, rotation)
        block_width = max(right - left, 1)
        block_height = max(bottom - top, 1)
        if block["type"] == "image":
            image_name = f"OEBPS/images/page-{index}-{block_index}.{block['ext']}"
            images.append((image_name, block["data"]))
            markup.append(f'<img class="pdf-image" src="images/page-{index}-{block_index}.{block["ext"]}" style="left:{left:.2f}px;top:{top:.2f}px;width:{block_width:.2f}px;height:{block_height:.2f}px;" alt="Page {index} image"/>')
            continue
        if block["type"] == "widget":
            markup.append(render_widget(block, left, top, block_width, block_height))
            continue
        if not include_text:
            continue
        for line_index, line in enumerate(block["lines"]):
            line_left, line_top, line_right, line_bottom = transform_bbox(line["bbox"], width, height, rotation)
            line_height = max(line_bottom - line_top, 1)
            inline_spans: list[str] = []
            for span_index, span in enumerate(line["spans"]):
                color = int(span["color"])
                rgb = f"#{(color >> 16) & 255:02x}{(color >> 8) & 255:02x}{color & 255:02x}"
                flags = int(span["flags"])
                weight = "700" if flags & 16 else "400"
                italic = "italic" if flags & 2 else "normal"
                inline_spans.append(
                    f'<span class="pdf-span" id="page-{index}-line-{line_index}-span-{span_index}" style="font-family:{escape(str(span["font"]))},sans-serif;font-size:{float(span["size"]):.2f}px;color:{rgb};font-weight:{weight};font-style:{italic};">{escape(str(span["text"]))}</span>'
                )
            markup.append(
                f'<div class="pdf-text" id="page-{index}-line-{line_index}" style="left:{line_left:.2f}px;top:{line_top:.2f}px;width:{max(line_right - line_left, 1):.2f}px;height:{line_height:.2f}px;line-height:{line_height:.2f}px;">{"".join(inline_spans)}</div>'
            )
    return "".join(markup), images


def render_widget(widget: dict[str, object], left: float, top: float, width: float, height: float) -> str:
    name = escape(str(widget["name"]), quote=True)
    value = escape(str(widget["value"]), quote=True)
    field_type = widget["field_type"]
    style = f"left:{left:.2f}px;top:{top:.2f}px;width:{width:.2f}px;height:{height:.2f}px;"
    if field_type == "checkbox":
        checked = " checked" if widget["checked"] else ""
        return f'<input class="pdf-widget pdf-checkbox" type="checkbox" name="{name}" value="{value or "on"}"{checked} style="{style}" aria-label="{name}"/>'
    if field_type == "radio":
        checked = " checked" if widget["checked"] else ""
        return f'<input class="pdf-widget pdf-radio" type="radio" name="{name}" value="{value or "on"}"{checked} style="{style}" aria-label="{name}"/>'
    if field_type == "select":
        options = "".join(
            f'<option value="{escape(str(choice), quote=True)}"{" selected" if str(choice) == str(widget["value"]) else ""}>{escape(str(choice))}</option>'
            for choice in widget["choices"]
        )
        return f'<select class="pdf-widget pdf-select" name="{name}" style="{style}" aria-label="{name}">{options}</select>'
    return f'<input class="pdf-widget pdf-input" type="text" name="{name}" value="{value}" style="{style}" aria-label="{name}"/>'


def transform_bbox(bbox: tuple[float, float, float, float], width: float, height: float, rotation: int) -> tuple[float, float, float, float]:
    left, top, right, bottom = bbox
    if rotation == 90:
        return height - bottom, left, height - top, right
    if rotation == 180:
        return width - right, height - bottom, width - left, height - top
    if rotation == 270:
        return top, width - right, bottom, width - left
    return left, top, right, bottom


TTS_WORKER_SCRIPT = Path(__file__).with_name("tts_worker.py")


def synthesize_speech(text: str, max_seconds: float | None = None) -> tuple[bytes, float]:
    # pyttsx3's SAPI5 driver can hang indefinitely inside engine.runAndWait() on some
    # Windows/COM configurations. A thread stuck in that native call cannot be killed,
    # so synthesis runs in a subprocess with a hard timeout that CAN be killed, bounding
    # the cost of any single page and letting the rest of the conversion complete.
    word_count = max(1, len(text.split()))
    timeout_seconds = min(120, max(30, word_count / 165 * 60 * 2 + 15))
    if max_seconds is not None:
        timeout_seconds = min(timeout_seconds, max_seconds)
    with tempfile.TemporaryDirectory() as directory:
        text_path = Path(directory) / "narration.txt"
        audio_path = Path(directory) / "narration.wav"
        text_path.write_text(text, encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, str(TTS_WORKER_SCRIPT), str(text_path), str(audio_path)],
                timeout=timeout_seconds,
                capture_output=True,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("The local text-to-speech engine timed out while narrating this page.") from error
        if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"The local text-to-speech engine did not produce audio.{f' ({stderr})' if stderr else ''}")
        audio_bytes = audio_path.read_bytes()
        with wave.open(str(audio_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        return audio_bytes, duration
