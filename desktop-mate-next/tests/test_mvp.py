import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from mate.schedule import Schedule
from mate.widget import DesktopMate


class ScheduleTests(unittest.TestCase):
    def test_countdown_boundaries(self):
        schedule = Schedule()
        self.assertEqual(schedule.display(datetime(2026, 1, 1, 8))[1], "출근 전이에요")
        self.assertEqual(schedule.display(datetime(2026, 1, 1, 12)), (
            "퇴근까지 06:00:00", "점심시간이에요"
        ))
        self.assertEqual(schedule.display(datetime(2026, 1, 1, 13))[1], "업무 중이에요")
        self.assertEqual(schedule.display(datetime(2026, 1, 1, 17, 59, 59, 999999))[0],
                         "퇴근까지 00:00:01")
        self.assertEqual(schedule.display(datetime(2026, 1, 1, 18))[0], "퇴근 시간이에요!")


class WidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = QSettings(str(self.root / "tests.ini"), QSettings.Format.IniFormat)

    def open_widget(self):
        widget = DesktopMate(self.settings, self.root)
        self.addCleanup(widget.close)
        widget.show()
        self.app.processEvents()
        return widget

    def test_missing_resource_and_click(self):
        widget = self.open_widget()
        self.assertTrue(widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(widget.animation.isHidden())
        self.assertIn("우클릭", widget.bubble.text())
        QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=widget.bubble.geometry().center())
        self.assertIn("조금만 더 힘내요!", widget.bubble.text())

    def test_visible_countdown_updates(self):
        with patch("mate.widget.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, 17, 0)
            widget = self.open_widget()
            self.assertIn("퇴근까지 01:00:00", widget.bubble.text())
            clock.now.return_value = datetime(2026, 1, 1, 17, 0, 1)
            tick = QSignalSpy(widget.clock.timeout)
            self.assertTrue(tick.wait(1000))
            self.assertIn("퇴근까지 00:59:59", widget.bubble.text())

    def test_bad_png_shows_diagnostic(self):
        image = self.root / "broken.png"
        image.write_bytes(b"invalid image")
        self.settings.setValue("asset", str(image))
        widget = self.open_widget()
        self.assertTrue(widget.animation.isHidden())
        self.assertIn("PNG를 읽을 수 없어요", widget.bubble.text())

    def test_png_frames_and_position(self):
        frames = self.root / "frames"
        frames.mkdir()
        for filename, color in (("frame10.png", "blue"), ("frame2.png", "red")):
            image = QImage(4, 4, QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.transparent)
            image.setPixelColor(2, 2, QColor(color))
            self.assertTrue(image.save(str(frames / filename)))
        widget = self.open_widget()
        animation = widget.animation
        self.assertTrue(animation.isVisible())
        self.assertEqual(animation._pixmap.toImage().pixelColor(2, 2), QColor("red"))
        timer = QSignalSpy(animation._timer.timeout)
        self.assertTrue(timer.wait(750))
        image = animation._pixmap.toImage()
        self.assertEqual(image.pixelColor(2, 2), QColor("blue"))
        self.assertEqual(image.pixelColor(0, 0).alpha(), 0)

        before = widget.pos()
        start = widget.bubble.geometry().center()
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(widget, start - QPoint(25, 20))
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=start)
        self.assertEqual(widget.pos(), before - QPoint(25, 20))
        self.assertEqual(self.settings.value("position"), widget.pos())

    def test_gif_advances(self):
        # Two 1-by-1 frames with different palette entries.
        gif = bytes.fromhex(
            "47494638396101000100800000000000ffffff"
            "21f90400050000002c0000000001000100000202440100"
            "21f90400050000002c00000000010001000002024c01003b"
        )
        (self.root / "character.gif").write_bytes(gif)
        widget = self.open_widget()
        movie = widget.animation._movie
        self.assertTrue(movie.isValid())
        frames = QSignalSpy(movie.frameChanged)
        self.assertTrue(frames.wait(750))
        self.assertEqual(frames.at(0), [1])
        self.assertTrue(frames.wait(750))
        self.assertEqual(frames.at(1), [0])


if __name__ == "__main__":
    unittest.main()
