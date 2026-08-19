import io
import re
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ElementTree
import pymupdf
import cv2
import numpy as np
from unittest.mock import MagicMock, patch

sys.path.insert(0, "services/api-gateway")
from app.main import (
    AzureExtractionError,
    build_epub,
    extract_pdf_pages,
    extract_with_azure_di,
    merge_azure_text,
    remove_ocr_text_from_background,
    remove_ocr_text_from_embedded_images,
)


class EpubGenerationTests(unittest.TestCase):
    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_all_pages_and_read_aloud_assets_are_written(self, _speech):
        pages = [
            {
                "width": 612,
                "height": 792,
                "rotation": 0,
                "text": "Heading paragraph",
                "page_image": b"page image",
                "media": [{"name": "sample.mp3", "data": b"audio bytes", "mime": "audio/mpeg", "kind": "audio"}],
                "blocks": [
                    {
                        "type": "text",
                        "bbox": (50, 70, 300, 100),
                        "lines": [
                            {
                                "bbox": (50, 70, 300, 100),
                                "spans": [
                                    {
                                        "text": "Heading",
                                        "font": "Helvetica-Bold",
                                        "size": 24,
                                        "color": 0,
                                        "flags": 16,
                                        "bbox": (50, 70, 160, 100),
                                    },
                                    {
                                        "text": " paragraph",
                                        "font": "Helvetica",
                                        "size": 12,
                                        "color": 0,
                                        "flags": 0,
                                        "bbox": (160, 70, 240, 100),
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
            {
                "width": 792,
                "height": 612,
                "rotation": 90,
                "text": "Second page",
                "blocks": [],
                "page_image": b"page image",
            },
        ]

        with zipfile.ZipFile(io.BytesIO(build_epub("Test book", pages, "fixed"))) as archive:
            names = archive.namelist()
            page = archive.read("OEBPS/page-1.xhtml").decode("utf-8")
            overlay = archive.read("OEBPS/overlay-1.smil").decode("utf-8")
            opf = archive.read("OEBPS/content.opf").decode("utf-8")

        self.assertIn("OEBPS/page-1.xhtml", names)
        self.assertIn("OEBPS/page-2.xhtml", names)
        self.assertIn("OEBPS/overlay-1.smil", names)
        self.assertIn("OEBPS/overlay-2.smil", names)
        self.assertIn("OEBPS/audio/page-1.wav", names)
        self.assertIn("OEBPS/audio/page-2.wav", names)
        self.assertEqual(opf.count("<itemref "), 2)
        self.assertIn('class="page fixed"', page)
        self.assertIn('class="page-text accessibility-text"', page)
        self.assertIn("<p>Heading paragraph</p>", page)
        self.assertIn("text src=", overlay)
        self.assertIn("audio src=", overlay)
        self.assertTrue(any(name.startswith("OEBPS/media/") and name.endswith("sample.mp3") for name in names))
        self.assertIn("<audio", page)
        self.assertIn("audio/mpeg", opf)

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_form_widgets_are_rendered(self, _speech):
        page = {
            "width": 612,
            "height": 792,
            "rotation": 0,
            "text": "Form page",
            "page_image": b"page image",
            "blocks": [
                {"type": "widget", "bbox": (50, 50, 250, 75), "field_type": "text", "name": "full_name", "value": "Rahul", "choices": [], "checked": False},
                {"type": "widget", "bbox": (50, 90, 70, 110), "field_type": "checkbox", "name": "approved", "value": "Yes", "choices": [], "checked": True},
                {"type": "widget", "bbox": (50, 120, 200, 145), "field_type": "select", "name": "role", "value": "Editor", "choices": ["Author", "Editor"], "checked": False},
            ],
        }
        with zipfile.ZipFile(io.BytesIO(build_epub("Form test", [page]))) as archive:
            content = archive.read("OEBPS/page-1.xhtml").decode("utf-8")
        self.assertIn('name="full_name"', content)
        self.assertIn('value="Rahul"', content)
        self.assertIn('type="checkbox"', content)
        self.assertIn('name="role"', content)
        self.assertIn("<option", content)

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_fixed_epub_has_valid_package_references(self, _speech):
        pages = [self._page("First page"), self._page("Second page")]
        with zipfile.ZipFile(io.BytesIO(build_epub("Fixed book", pages, "fixed"))) as archive:
            names = set(archive.namelist())
            mimetype = archive.read("mimetype")
            container = archive.read("META-INF/container.xml")
            opf = archive.read("OEBPS/content.opf")
            nav = archive.read("OEBPS/nav.xhtml")

            ElementTree.fromstring(container)
            package = ElementTree.fromstring(opf)
            nav_document = ElementTree.fromstring(nav)

        self.assertEqual(mimetype, b"application/epub+zip")
        self.assertIn("OEBPS/page-1.xhtml", names)
        self.assertIn("OEBPS/page-2.xhtml", names)
        self.assertIn("OEBPS/overlay-1.smil", names)
        self.assertIn("OEBPS/overlay-2.smil", names)
        self.assertIn("OEBPS/audio/page-1.wav", names)
        self.assertIn("OEBPS/audio/page-2.wav", names)
        self.assertEqual(package.tag, "{http://www.idpf.org/2007/opf}package")
        self.assertEqual(len(package.findall("{http://www.idpf.org/2007/opf}spine/{http://www.idpf.org/2007/opf}itemref")), 2)
        self.assertIn(b"media-overlay=\"overlay-1\"", opf)
        self.assertIn(b"media-overlay=\"overlay-2\"", opf)
        self.assertIn(b"Page 2", nav)
        self.assertEqual(nav_document.tag, "{http://www.w3.org/1999/xhtml}html")

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_reflowable_epub_contains_readable_content(self, _speech):
        page = self._page("Readable paragraph")
        with zipfile.ZipFile(io.BytesIO(build_epub("Reflowable book", [page], "reflowable"))) as archive:
            page_content = archive.read("OEBPS/page-1.xhtml").decode("utf-8")
            opf = archive.read("OEBPS/content.opf").decode("utf-8")
        self.assertIn("Readable paragraph", page_content)
        self.assertIn("class=\"page reflowable\"", page_content)
        self.assertNotIn("pdf-page-background", page_content)
        self.assertIn("<meta property=\"rendition:layout\">reflowable</meta>", opf)

    def test_azure_text_merges_into_pdf_pages(self):
        pages = [self._page("Local text")]
        merged = merge_azure_text(
            pages,
            [{"text": "Azure extracted text", "lines": [{"text": "Azure extracted text", "spans": []}]}],
        )
        self.assertEqual(merged[0]["text"], "Azure extracted text")
        self.assertEqual(merged[0]["azure_lines"][0]["text"], "Azure extracted text")

    def test_background_text_is_still_erased_when_azure_omits_page_dimensions(self):
        # DocumentPage.width/height are optional in the Azure SDK and can come back None for
        # a page even when it has text - dict.get(key, default) does not fall back in that
        # case (the key is present, just holding None), which used to leave azure_width/height
        # as None and made remove_ocr_text_from_background bail out, keeping the original
        # (uncleaned) background image with all of its text still baked in.
        document = pymupdf.open()
        page = document.new_page(width=400, height=300)
        page.insert_text((20, 40), "A line of text that must be erased from the background")
        source = document.tobytes()
        document.close()

        pages = extract_pdf_pages(source)
        line_bbox = pages[0]["blocks"][0]["lines"][0]["bbox"]
        left, top, right, bottom = line_bbox
        azure_pages = [{
            "text": "A line of text that must be erased from the background",
            "lines": [{"text": "line", "polygon": [left, top, right, top, right, bottom, left, bottom]}],
            "words": [],
            "width": None,
            "height": None,
        }]

        merged = merge_azure_text(pages, azure_pages)

        # The background PNG is rendered at 2x zoom (see extract_pdf_pages), so the bbox
        # (in PDF points) must be scaled up to index the actual pixel array.
        cleaned = cv2.imdecode(np.frombuffer(merged[0]["page_image"], dtype=np.uint8), cv2.IMREAD_COLOR)
        zoom = cleaned.shape[1] / pages[0]["width"]
        crop = cleaned[int(top * zoom):int(bottom * zoom), int(left * zoom):int(right * zoom)]
        self.assertGreater(int(np.mean(crop)), 200)

    def test_azure_configuration_error_is_explicit(self):
        self.assertTrue(issubclass(AzureExtractionError, RuntimeError))

    @patch.dict("app.main.os.environ", {
        "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://example.cognitiveservices.azure.com/",
        "AZURE_DOCUMENT_INTELLIGENCE_KEY": "test-key",
    })
    @patch("app.main.DocumentIntelligenceClient")
    def test_azure_pdf_is_sent_as_application_pdf(self, client_class):
        client = client_class.return_value
        result_page = MagicMock()
        result_page.lines = [MagicMock(content="Azure line")]
        client.begin_analyze_document.return_value.result.return_value.pages = [result_page]

        pages = extract_with_azure_di(b"%PDF-test")

        call = client.begin_analyze_document.call_args
        self.assertEqual(call.args[0], "prebuilt-layout")
        self.assertEqual(call.kwargs["content_type"], "application/pdf")
        self.assertEqual(call.kwargs["body"].read(), b"%PDF-test")
        self.assertEqual(pages[0]["text"], "Azure line")

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_complete_pdf_text_is_written_to_oebps_html(self, _speech):
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((40, 60), "First complete line")
        page.insert_text((40, 90), "Second complete line")
        source = document.tobytes()
        document.close()

        pages = extract_pdf_pages(source)
        with zipfile.ZipFile(io.BytesIO(build_epub("Complete text", pages, "reflowable"))) as archive:
            html = archive.read("OEBPS/page-1.xhtml").decode("utf-8")

        self.assertIn("First complete line", html)
        self.assertIn("Second complete line", html)

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_background_image_ocr_text_is_written_to_fixed_html(self, _speech):
        page = self._page("OCR text from background image")
        page["azure_lines"] = [{"text": "OCR text from background image", "spans": []}]
        page["background_image_text"] = "OCR text from background image"

        with zipfile.ZipFile(io.BytesIO(build_epub("Image OCR", [page], "fixed"))) as archive:
            html = archive.read("OEBPS/page-1.xhtml").decode("utf-8")

        self.assertIn("OCR text from background image", html)
        self.assertIn("class=\"page-text accessibility-text\"", html)

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_azure_ocr_lines_are_placed_within_rotated_container(self, _speech):
        # page["width"]/["height"] are the PDF's unrotated cropbox dimensions (portrait,
        # 200x300); the page is rotated 90 degrees, so the fixed-layout container becomes
        # landscape (300x200) to match the background image. Azure reports its polygons in
        # that same visual/landscape space, as it does for a real rotated scan.
        page = {
            "width": 200,
            "height": 300,
            "rotation": 90,
            "text": "HELLO\nWORLD",
            "page_image": b"page image",
            "media": [],
            "blocks": [],
            "azure_lines": [
                {"text": "HELLO", "polygon": [10, 10, 110, 10, 110, 40, 10, 40]},
                {"text": "WORLD", "polygon": [10, 50, 110, 50, 110, 80, 10, 80]},
            ],
            "azure_width": 300,
            "azure_height": 200,
        }

        with zipfile.ZipFile(io.BytesIO(build_epub("Rotated OCR", [page], "fixed"))) as archive:
            html = archive.read("OEBPS/page-1.xhtml").decode("utf-8")

        # The container must match the rotated (landscape) visual size, not the raw portrait
        # cropbox - otherwise the background image renders stretched into the wrong shape.
        self.assertIn('style="width:300.00px;height:200.00px;"', html)

        boxes = []
        for match in re.finditer(r'<div class="ocr-line"[^>]*style="([^"]*)"', html):
            style = dict(item.split(":") for item in match.group(1).rstrip(";").split(";"))
            boxes.append({key: float(value.rstrip("px")) for key, value in style.items() if key != "line-height"})
        self.assertEqual(len(boxes), 2)
        for box in boxes:
            # Both lines must land inside the rotated 300x200 container...
            self.assertLessEqual(box["left"] + box["width"], 300)
            self.assertLessEqual(box["top"] + box["height"], 200)
        # ...and not on top of each other.
        first, second = sorted(boxes, key=lambda box: box["top"])
        self.assertLessEqual(first["top"] + first["height"], second["top"])

    def test_ocr_polygon_removes_background_text_pixels(self):
        image = np.full((100, 200, 3), 255, dtype=np.uint8)
        cv2.putText(image, "TEXT", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 3)
        success, encoded = cv2.imencode(".png", image)
        self.assertTrue(success)
        cleaned = remove_ocr_text_from_background(
            encoded.tobytes(), [{"polygon": [25, 35, 90, 35, 90, 70, 25, 70]}], 100, 50
        )
        cleaned_image = cv2.imdecode(np.frombuffer(cleaned, dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(np.mean(cleaned_image[35:70, 50:90])), 180)

    def test_embedded_page_image_ocr_text_is_erased(self):
        image = np.full((100, 200, 3), 255, dtype=np.uint8)
        cv2.putText(image, "SCAN", (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 3)
        success, encoded = cv2.imencode(".png", image)
        self.assertTrue(success)

        blocks = [{"type": "image", "bbox": (0, 0, 200, 100), "data": encoded.tobytes(), "ext": "png"}]
        # Word polygon in Azure's page-coordinate space (azure page is 100x50, half the
        # block's own pixel size), landing on the same text drawn above.
        ocr_regions = [{"polygon": [12.5, 17.5, 45, 17.5, 45, 35, 12.5, 35]}]

        remove_ocr_text_from_embedded_images(blocks, ocr_regions, 200, 100, 100, 50)

        self.assertEqual(blocks[0]["ext"], "png")
        cleaned_image = cv2.imdecode(np.frombuffer(blocks[0]["data"], dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertGreater(int(np.mean(cleaned_image[35:70, 50:90])), 180)

    def test_embedded_page_image_without_overlapping_text_is_untouched(self):
        original_bytes = b"not-a-real-image"
        blocks = [{"type": "image", "bbox": (0, 0, 200, 100), "data": original_bytes, "ext": "jpg"}]
        # Polygon sits entirely outside the block's bbox once mapped to page coordinates.
        ocr_regions = [{"polygon": [500, 500, 520, 500, 520, 520, 500, 520]}]

        remove_ocr_text_from_embedded_images(blocks, ocr_regions, 200, 100, 100, 50)

        self.assertEqual(blocks[0]["data"], original_bytes)
        self.assertEqual(blocks[0]["ext"], "jpg")

    @patch("app.main.synthesize_speech", return_value=(b"audio", 1.0))
    def test_fixed_html_keeps_extracted_image_reference(self, _speech):
        page = self._page("Page with image")
        page["blocks"].append({
            "type": "image",
            "bbox": (80, 120, 240, 240),
            "data": b"png-bytes",
            "ext": "png",
        })

        with zipfile.ZipFile(io.BytesIO(build_epub("Image page", [page], "fixed"))) as archive:
            names = archive.namelist()
            html = archive.read("OEBPS/page-1.xhtml").decode("utf-8")

        self.assertIn("OEBPS/images/page-1-1.png", names)
        self.assertIn('src="images/page-1-1.png"', html)

    @staticmethod
    def _page(text: str) -> dict[str, object]:
        return {
            "width": 612,
            "height": 792,
            "rotation": 0,
            "text": text,
            "page_image": b"page image",
            "media": [],
            "blocks": [{
                "type": "text",
                "bbox": (50, 70, 300, 100),
                "lines": [{
                    "bbox": (50, 70, 300, 100),
                    "spans": [{
                        "text": text,
                        "font": "Helvetica",
                        "size": 12,
                        "color": 0,
                        "flags": 0,
                        "bbox": (50, 70, 300, 100),
                    }],
                }],
            }],
        }


if __name__ == "__main__":
    unittest.main()
