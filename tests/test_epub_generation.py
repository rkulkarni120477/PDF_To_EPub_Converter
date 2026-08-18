import io
import sys
import unittest
import zipfile
import xml.etree.ElementTree as ElementTree
from unittest.mock import MagicMock, patch

sys.path.insert(0, "services/api-gateway")
from app.main import AzureExtractionError, build_epub, extract_with_azure_di, merge_azure_text


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
