import asyncio
from html import escape
from io import BytesIO
import os
from pathlib import Path
import re
import tempfile
from uuid import uuid4
import wave
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, StreamingResponse
import pymupdf as fitz
import pyttsx3
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="PDF to ePub API Gateway")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:4173", "http://127.0.0.1:4173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CONVERSIONS: dict[str, tuple[str, bytes]] = {}


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


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, object]:
    return {
        "service": "api-gateway",
        "capabilities": ["projects", "conversions", "downloads"],
        "azure_document_intelligence_configured": azure_di_configured(),
    }


@app.post("/api/v1/conversions")
async def convert_pdf(file: UploadFile | None = File(None), layout: str = Form("auto"), use_azure_di: bool = Form(False)) -> dict[str, str | int | bool]:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="A PDF file is required.")
    if Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    source = await file.read()
    try:
        pages = await asyncio.wait_for(asyncio.to_thread(extract_pdf_pages, source), timeout=60)
    except (fitz.FileDataError, ValueError) as error:
        raise HTTPException(status_code=400, detail="The uploaded file is not a readable PDF.") from error
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=504, detail="PDF layout extraction timed out.") from error
    if use_azure_di:
        try:
            azure_pages = await asyncio.wait_for(asyncio.to_thread(extract_with_azure_di, source), timeout=120)
            pages = merge_azure_text(pages, azure_pages)
        except AzureExtractionError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except asyncio.TimeoutError as error:
            raise HTTPException(status_code=504, detail="Azure Document Intelligence extraction timed out.") from error
    conversion_id = uuid4().hex
    title = Path(file.filename).stem.replace("_", " ").replace("-", " ").strip() or "Converted document"
    try:
        epub = await asyncio.wait_for(asyncio.to_thread(build_epub, title, pages, layout), timeout=180)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except asyncio.TimeoutError as error:
        raise HTTPException(status_code=504, detail="EPUB generation timed out. Try a smaller PDF or fewer pages.") from error
    CONVERSIONS[conversion_id] = (f"{title}.epub", epub)
    return {
        "status": "completed",
        "conversion_id": conversion_id,
        "title": title,
        "pages": len(pages),
        "layout": resolve_layout(layout, pages),
        "azure_document_intelligence": use_azure_di,
        "download_url": f"/api/v1/downloads/{conversion_id}",
    }


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
        client = DocumentIntelligenceClient(endpoint=endpoint, credential=AzureKeyCredential(key))
        poller = client.begin_analyze_document("prebuilt-layout", body=source)
        result = poller.result()
    except Exception as error:
        raise AzureExtractionError("Azure Document Intelligence could not extract this PDF.") from error
    pages: list[dict[str, object]] = []
    for page in result.pages or []:
        lines = [{"text": line.content, "spans": []} for line in (page.lines or []) if line.content.strip()]
        pages.append({"text": "\n".join(line["text"] for line in lines), "lines": lines})
    return pages


def merge_azure_text(pages: list[dict[str, object]], azure_pages: list[dict[str, object]]) -> list[dict[str, object]]:
    for index, azure_page in enumerate(azure_pages):
        if index >= len(pages) or not azure_page["text"]:
            continue
        pages[index]["text"] = azure_page["text"]
        pages[index]["azure_lines"] = azure_page["lines"]
    return pages


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
    return {"status": "cleared", "deleted": deleted_count}


def extract_pdf_pages(source: bytes) -> list[dict[str, object]]:
    document = fitz.open(stream=source, filetype="pdf")
    pages: list[dict[str, object]] = []
    document_media = extract_document_media(document)
    try:
        for page in document:
            page_dict = page.get_text("dict")
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
                "width": page.rect.width,
                "height": page.rect.height,
                "rotation": page.rotation,
                "blocks": blocks,
                "text": " ".join(page_text).strip(),
                "page_image": page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png"),
                "media": media,
            })
    finally:
        document.close()
    return pages


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


def build_epub(title: str, pages: list[dict[str, object]], layout: str = "auto") -> bytes:
    resolved_layout = resolve_layout(layout, pages)
    fixed_layout = resolved_layout == "fixed"
    page_documents: list[tuple[str, str]] = []
    overlay_documents: list[tuple[str, str]] = []
    audio_files: list[tuple[str, bytes]] = []
    image_files: list[tuple[str, bytes, str]] = []
    media_files: list[tuple[str, bytes, str]] = []
    for index, page in enumerate(pages, start=1):
        page_text = str(page["text"]).strip() or "This page did not contain extractable text."
        width = float(page["width"])
        height = float(page["height"])
        rotation = int(page["rotation"])
        page_width, page_height = (height, width) if rotation in (90, 270) else (width, height)
        if fixed_layout:
            markup, page_images = render_page(page, index, width, height, rotation)
            markup = render_fixed_accessibility_layer(page, index)
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
            (page_name, f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{escape(title)} - Page {index}</title><meta charset="utf-8"/><meta name="viewport" content="width={page_width:.0f}, height={page_height:.0f}"/><link rel="stylesheet" type="text/css" href="style.css"/></head><body><section id="page-{index}" class="page {resolved_layout}" style="{f"width:{page_width:.2f}px;height:{page_height:.2f}px;" if fixed_layout else ""}" data-rotation="{rotation}"><div id="page-{index}-text" class="page-text">{markup}</div></section></body></html>''')
        )
        image_files.extend((name, data, Path(name).suffix.lstrip(".")) for name, data in page_images)
        media_files.extend(page_media_files)
        audio_name = f"OEBPS/audio/page-{index}.wav"
        audio_bytes, duration = synthesize_speech(page_text)
        audio_files.append((audio_name, audio_bytes))
        overlay_documents.append(
            (f"overlay-{index}.smil", f'''<?xml version="1.0" encoding="UTF-8"?><smil xmlns="http://www.w3.org/ns/SMIL" version="3.0"><body><par id="page-{index}-par"><text src="{page_name}#page-{index}-text"/><audio src="audio/page-{index}.wav" clipBegin="0.0" clipEnd="{duration:.3f}"/></par></body></smil>''')
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
    style = ".-epub-media-overlay-active { background: #fff2a8 !important; } .page { margin: 0 auto; } .page.fixed { position: relative; overflow: hidden; page-break-after: always; } .page.reflowable { max-width: 48rem; padding: 2rem 1.5rem; } .fixed .page-text { position: absolute; inset: 0; z-index: 2; } .fixed .accessibility-text { width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: normal; border: 0; } .fixed .accessibility-text p { margin: 0; } .pdf-page-background { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: fill; z-index: 1; } .pdf-span { display: inline; vertical-align: baseline; } .reflow-image { display: block; max-width: 100%; height: auto; margin: 1rem auto; } .reflow-paragraph { margin: 0 0 .75rem; line-height: 1.45; } .reflow-field { display: block; margin: .75rem 0; color: #17232b; } .reflow-field input, .reflow-field select { margin-left: .5rem; padding: .35rem; } .pdf-widget { position: absolute; z-index: 4; box-sizing: border-box; font: inherit; color: #111; background: rgba(255,255,255,.88); border: 1px solid #4d6670; padding: 2px 4px; } .pdf-checkbox, .pdf-radio { padding: 0; accent-color: #0c7770; background: rgba(255,255,255,.95); } .pdf-select { padding: 0 2px; } .pdf-media { z-index: 5; background: rgba(255,255,255,.95); }"
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
    for block_index, block in enumerate(page["blocks"]):
        if block["type"] == "image":
            image_name = f"OEBPS/images/page-{index}-{block_index}.{block['ext']}"
            images.append((image_name, block["data"]))
            markup.append(f'<img class="reflow-image" src="images/page-{index}-{block_index}.{block["ext"]}" alt="Page {index} image"/>')
        elif block["type"] == "widget":
            markup.append(render_reflow_widget(block))
        elif block["type"] == "text" and not azure_lines:
            for line_index, line in enumerate(block["lines"]):
                spans = []
                for span in line["spans"]:
                    color = int(span["color"])
                    rgb = f"#{(color >> 16) & 255:02x}{(color >> 8) & 255:02x}{color & 255:02x}"
                    flags = int(span["flags"])
                    weight = "700" if flags & 16 else "400"
                    italic = "italic" if flags & 2 else "normal"
                    spans.append(f'<span class="pdf-span" id="page-{index}-line-{line_index}" style="font-family:{escape(str(span["font"]))},sans-serif;font-size:{float(span["size"]):.2f}px;color:{rgb};font-weight:{weight};font-style:{italic};">{escape(str(span["text"]))}</span>')
                markup.append(f'<p class="reflow-paragraph">{"".join(spans)}</p>')
    if not markup:
        markup.append('<p class="reflow-paragraph">No extractable text on this page.</p>')
    return "".join(markup), images


def render_fixed_accessibility_layer(page: dict[str, object], index: int) -> str:
    paragraphs: list[str] = []
    for block in page["blocks"]:
        if block["type"] != "text":
            continue
        for line in block["lines"]:
            text = "".join(str(span["text"]) for span in line["spans"])
            if text.strip():
                paragraphs.append(f"<p>{escape(text)}</p>")
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


def render_page(page: dict[str, object], index: int, width: float, height: float, rotation: int) -> tuple[str, list[tuple[str, bytes]]]:
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


def synthesize_speech(text: str) -> tuple[bytes, float]:
    with tempfile.TemporaryDirectory() as directory:
        audio_path = Path(directory) / "narration.wav"
        engine = pyttsx3.init()
        engine.setProperty("rate", 165)
        engine.save_to_file(text, str(audio_path))
        engine.runAndWait()
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            raise RuntimeError("The local text-to-speech engine did not produce audio.")
        audio_bytes = audio_path.read_bytes()
        with wave.open(str(audio_path), "rb") as audio:
            duration = audio.getnframes() / audio.getframerate()
        return audio_bytes, duration
