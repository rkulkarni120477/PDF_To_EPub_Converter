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


if __name__ == "__main__":
    unittest.main()
