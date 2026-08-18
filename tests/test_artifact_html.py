"""HTML 产物预览与离线下载回归测试。"""
import base64
import tempfile
import unittest
from pathlib import Path

from services.artifact_service import build_preview_html, build_standalone_html


class ArtifactHtmlTests(unittest.TestCase):
    def test_preview_html_keeps_relative_resources_and_enables_overlay(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            (assets / "screen.png").write_bytes(b"test-image-content")
            report = root / "report.html"
            report.write_text(
                "<html><head></head><body>"
                '<link rel="stylesheet" href="assets/style.css">'
                '<a href="assets/screen.png"><img src="assets/screen.png"></a>'
                "</body></html>",
                encoding="utf-8",
            )

            content = build_preview_html(report, "/api/artifacts/13/content/")

            self.assertIn('<base href="/api/artifacts/13/content/">', content)
            self.assertIn('href="javascript:void(0)"', content)
            self.assertIn('data-preview-src="assets/screen.png"', content)
            self.assertIn('src="assets/screen.png"', content)
            self.assertIn('href="assets/style.css"', content)
            self.assertIn("artifact-image-preview-overlay", content)
            self.assertIn("artifact-image-preview-close", content)
            self.assertIn("artifact-image-preview-request", content)
            self.assertIn("window.parent.postMessage", content)

    def test_standalone_html_inlines_images_and_stylesheet_resources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            assets = root / "assets"
            assets.mkdir()
            image_bytes = b"test-image-content"
            (assets / "screen.png").write_bytes(image_bytes)
            (assets / "style.css").write_text(
                '.shot { background-image: url("screen.png"); }',
                encoding="utf-8",
            )
            report = root / "report.html"
            report.write_text(
                '<link rel="stylesheet" href="assets/style.css">'
                '<a href="assets/screen.png"><img src="assets/screen.png"></a>'
                '<img src="https://example.com/remote.png">',
                encoding="utf-8",
            )

            content = build_standalone_html(report)

            encoded_image = base64.b64encode(image_bytes).decode("ascii")
            inlined_css = f'.shot {{ background-image: url("data:image/png;base64,{encoded_image}"); }}'
            encoded_css = base64.b64encode(inlined_css.encode("utf-8")).decode("ascii")
            self.assertNotIn('href="assets/style.css"', content)
            self.assertNotIn('href="assets/screen.png"', content)
            self.assertNotIn('src="assets/screen.png"', content)
            self.assertIn(f"data:text/css;base64,{encoded_css}", content)
            self.assertEqual(content.count(f"data:image/png;base64,{encoded_image}"), 2)
            self.assertIn('href="javascript:void(0)"', content)
            self.assertIn(f'data-preview-src="data:image/png;base64,{encoded_image}"', content)
            self.assertIn("artifact-image-preview-overlay", content)
            self.assertIn("artifact-image-preview-stage", content)
            self.assertIn("artifact-image-preview-close", content)
            self.assertNotIn("artifact-image-preview-zoom-in", content)
            self.assertNotIn("artifact-image-preview-zoom-out", content)
            self.assertNotIn("artifact-image-preview-fit", content)
            self.assertNotIn("artifact-image-preview-reset", content)
            self.assertIn("artifact-image-preview-request", content)
            self.assertIn("setScale(", content)
            self.assertIn('stage.addEventListener("wheel"', content)
            self.assertIn('stage.addEventListener("pointermove"', content)
            self.assertIn('src="https://example.com/remote.png"', content)

    def test_standalone_html_does_not_read_outside_artifact_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact_dir = root / "artifact"
            artifact_dir.mkdir()
            (root / "outside.png").write_bytes(b"outside")
            report = artifact_dir / "report.html"
            report.write_text('<img src="../outside.png">', encoding="utf-8")

            content = build_standalone_html(report)

            self.assertIn('src="../outside.png"', content)
            self.assertNotIn(base64.b64encode(b"outside").decode("ascii"), content)


if __name__ == "__main__":
    unittest.main()
