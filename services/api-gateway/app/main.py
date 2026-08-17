from html import escape
from io import BytesIO
from pathlib import Path
import tempfile
from uuid import uuid4
import wave
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import StreamingResponse
import pymupdf as fitz
import pyttsx3

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
    return {"service": "api-gateway", "capabilities": ["projects", "conversions", "downloads"]}


@app.post("/api/v1/conversions")
async def convert_pdf(file: UploadFile | None = File(None)) -> dict[str, str | int]:
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="A PDF file is required.")
    if Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    source = await file.read()
    try:
        pages = extract_pdf_pages(source)
    except (fitz.FileDataError, ValueError) as error:
        raise HTTPException(status_code=400, detail="The uploaded file is not a readable PDF.") from error
    conversion_id = uuid4().hex
    title = Path(file.filename).stem.replace("_", " ").replace("-", " ").strip() or "Converted document"
    try:
        epub = build_epub(title, pages)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    CONVERSIONS[conversion_id] = (f"{title}.epub", epub)
    return {
        "status": "completed",
        "conversion_id": conversion_id,
        "title": title,
        "pages": len(pages),
        "download_url": f"/api/v1/downloads/{conversion_id}",
    }


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


def extract_pdf_pages(source: bytes) -> list[dict[str, object]]:
    document = fitz.open(stream=source, filetype="pdf")
    pages: list[dict[str, object]] = []
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
            pages.append({
                "width": page.rect.width,
                "height": page.rect.height,
                "rotation": page.rotation,
                "blocks": blocks,
                "text": " ".join(page_text).strip(),
                "page_image": page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png"),
            })
    finally:
        document.close()
    return pages


def build_epub(title: str, pages: list[dict[str, object]]) -> bytes:
    page_documents: list[tuple[str, str]] = []
    overlay_documents: list[tuple[str, str]] = []
    audio_files: list[tuple[str, bytes]] = []
    image_files: list[tuple[str, bytes, str]] = []
    for index, page in enumerate(pages, start=1):
        page_text = str(page["text"]).strip() or "This page did not contain extractable text."
        width = float(page["width"])
        height = float(page["height"])
        rotation = int(page["rotation"])
        page_width, page_height = (height, width) if rotation in (90, 270) else (width, height)
        markup, page_images = render_page(page, index, width, height, rotation)
        background_name = f"OEBPS/images/page-{index}-background.png"
        image_files.append((background_name, page["page_image"], "png"))
        markup = f'<img class="pdf-page-background" src="images/page-{index}-background.png" alt="" aria-hidden="true"/>{markup}'
        page_name = f"page-{index}.xhtml"
        page_documents.append(
            (page_name, f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{escape(title)} - Page {index}</title><meta charset="utf-8"/><meta name="viewport" content="width={page_width:.0f}, height={page_height:.0f}"/><link rel="stylesheet" type="text/css" href="style.css"/></head><body><section id="page-{index}" class="page" style="width:{page_width:.2f}px;height:{page_height:.2f}px;" data-rotation="{rotation}"><div id="page-{index}-text" class="page-text">{markup}</div></section></body></html>''')
        )
        image_files.extend((name, data, Path(name).suffix.lstrip(".")) for name, data in page_images)
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
    spine = "".join(f'<itemref idref="page-{index}" media-overlay="overlay-{index}"/>' for index in range(1, len(page_documents) + 1))
    opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">urn:uuid:{uuid4()}</dc:identifier><dc:title>{safe_title}</dc:title><dc:language>en</dc:language><meta property="rendition:layout">pre-paginated</meta><meta property="rendition:orientation">auto</meta><meta property="rendition:spread">none</meta><meta property="media:active-class">-epub-media-overlay-active</meta></metadata>
    <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="style" href="style.css" media-type="text/css"/>{page_manifest}{overlay_manifest}{audio_manifest}{image_manifest}</manifest>
    <spine>{spine}</spine>
</package>'''
    nav_items = "".join(f'<li><a href="{name}">Page {index}</a></li>' for index, (name, _) in enumerate(page_documents, start=1))
    nav = f'''<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE html><html xmlns="http://www.w3.org/1999/xhtml"><head><title>{safe_title}</title></head><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><h1>Contents</h1><ol>{nav_items}</ol></nav></body></html>'''
    style = ".-epub-media-overlay-active { background: #fff2a8 !important; } .page { position: relative; overflow: hidden; page-break-after: always; } .page-text { position: absolute; inset: 0; z-index: 2; } .pdf-page-background { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: fill; z-index: 1; } .pdf-text { position: absolute; white-space: pre; overflow: visible; color: transparent !important; } .pdf-span { display: inline; vertical-align: baseline; color: transparent !important; } .pdf-span.-epub-media-overlay-active { color: transparent !important; background: #fff2a8 !important; } .pdf-image { display: none; }"
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
    return output.getvalue()


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
