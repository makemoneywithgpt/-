import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QMovie, QPainter, QPixmap
from PySide6.QtWidgets import QWidget


def frame_order(path: Path) -> list[str | int]:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"([0-9]+)", path.name)]


class Animation(QWidget):
    unavailable = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._movie = QMovie(self)
        self._movie.frameChanged.connect(self.update)
        self._movie.finished.connect(self._restart_movie, Qt.ConnectionType.QueuedConnection)
        self._movie.error.connect(self._movie_error)
        self._timer = QTimer(self, interval=120)
        self._timer.timeout.connect(self._advance_png)
        self._frames: list[Path] = []
        self._index = 0
        self._pixmap = QPixmap()
        self._movie_active = False

    def load(self, path: Path) -> None:
        self.stop()
        self._movie.setFileName("")
        self._frames = []
        self._index = 0
        self._pixmap = QPixmap()

        if path.is_dir():
            self._frames = sorted(
                (entry for entry in path.iterdir() if entry.is_file() and entry.suffix.lower() == ".png"),
                key=frame_order,
            )
        elif path.is_file() and path.suffix.lower() == ".png":
            self._frames = [path]
        elif path.is_file() and path.suffix.lower() == ".gif":
            self._movie.setFileName(str(path))
            if self._movie.isValid():
                self._movie_active = True
                self._movie.start()
                self.update()
                return

        if self._frames:
            if self._read_png() and len(self._frames) > 1:
                self._timer.start()
        else:
            self.unavailable.emit("설정에서 GIF 또는 PNG 프레임을 선택하세요.")
        self.update()

    def stop(self) -> None:
        self._movie_active = False
        self._movie.stop()
        self._timer.stop()

    @Slot()
    def _restart_movie(self) -> None:
        if (self._movie_active and self._movie.state() == QMovie.MovieState.NotRunning
                and self._movie.frameCount() != 1):
            self._movie.setFileName(self._movie.fileName())
            self._movie.start()

    @Slot()
    def _movie_error(self) -> None:
        if self._movie_active:
            self._movie_active = False
            self.unavailable.emit("GIF를 읽을 수 없어요. 설정에서 파일을 확인하세요.")
            self.update()

    def _read_png(self) -> bool:
        pixmap = QPixmap(str(self._frames[self._index]))
        if pixmap.isNull():
            self._timer.stop()
            self._pixmap = QPixmap()
            self.unavailable.emit(f"PNG를 읽을 수 없어요. {self._frames[self._index].name}")
            return False
        self._pixmap = pixmap
        return True

    def _advance_png(self) -> None:
        self._index = (self._index + 1) % len(self._frames)
        self._read_png()
        self.update()

    def paintEvent(self, _event) -> None:
        pixmap = self._movie.currentPixmap() if self._movie_active else self._pixmap
        if pixmap.isNull():
            return
        size = pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        x = (self.width() - size.width()) // 2
        y = self.height() - size.height()
        painter.drawPixmap(x, y, size.width(), size.height(), pixmap)
