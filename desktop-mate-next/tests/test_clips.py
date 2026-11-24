import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from mate.clips import Pack
from mate.player import ActionPlayer
from tools.verify_bundle import check_animation


class ClipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.assets = Path(temporary.name)
        self.directory = self.assets / "actions"
        self.directory.mkdir()
        self.manifest = {
            "canvas": [8, 8],
            "clips": {
                "idle": [{"file": "one.png", "ms": 510}, {"file": "two.png", "ms": 70}],
                "drink": [{"file": "two.png", "ms": 130}],
                "doze": [{"file": "one.png", "ms": 920}],
            },
        }
        self.write_manifest()
        for path, color in (("one.png", "red"), ("two.png", "blue")):
            self.write_image(path, color)

    def write_manifest(self):
        (self.directory / "pack.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def write_image(self, path, color, point=(4, 4)):
        image = QImage(8, 8, QImage.Format.Format_ARGB32)
        image.fill(0)
        image.setPixelColor(*point, QColor(color))
        self.assertTrue(image.save(str(self.directory / path)))

    def test_original_durations_and_action_return(self):
        rendered = []
        player = ActionPlayer(rendered.append)
        self.addCleanup(player.stop)
        player.set_pack(Pack.read(self.directory))
        self.assertEqual(player.frame_timer.interval(), 510)
        self.assertTrue(90_000 <= player.special_timer.interval() <= 180_000)
        player.advance()
        self.assertEqual(player.frame_timer.interval(), 70)
        player.play_next_special()
        self.assertEqual(player.action, "drink")
        self.assertEqual(player.frame_timer.interval(), 130)
        self.assertFalse(player.special_timer.isActive())
        player.advance()
        self.assertEqual(player.action, "idle")
        self.assertEqual(player.index, 0)
        player.play_next_special()
        self.assertEqual(player.action, "doze")
        self.assertEqual(player.frame_timer.interval(), 920)
        self.assertTrue(all(frame.width() == 8 and frame.height() == 8 for frame in rendered))

    def test_pack_report_and_edge_validation(self):
        summary = check_animation(self.assets)
        self.assertEqual(summary["clips"]["idle"], {"frames": 2, "ms": 580})
        self.assertEqual(summary["frames"], 4)
        self.write_image("two.png", "blue", (0, 4))
        with self.assertRaisesRegex(ValueError, "transparent padding"):
            check_animation(self.assets)

    def test_missing_frame_is_rejected(self):
        (self.directory / "two.png").unlink()
        with self.assertRaisesRegex(ValueError, "two.png"):
            Pack.read(self.directory)


if __name__ == "__main__":
    unittest.main()
