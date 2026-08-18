import io
import sys
import unittest
import zipfile
from unittest.mock import patch

sys.path.insert(0, "services/api-gateway")
from app.main import build_epub


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

        with zipfile.ZipFile(io.BytesIO(build_epub("Test book", pages))) as archive:
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
        self.assertGreaterEqual(page.count('class="pdf-text"'), 1)
        self.assertGreaterEqual(page.count('class="pdf-span"'), 2)
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


if __name__ == "__main__":
    unittest.main()
