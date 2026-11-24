from datetime import datetime, time
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt, QTime, QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
    QMenu, QMessageBox, QPushButton, QSpinBox, QTimeEdit, QVBoxLayout, QWidget,
)

from mate.animation import Animation
from mate.clips import ACTIONS, Pack
from mate.player import ActionPlayer
from mate.schedule import Schedule


class Preferences(QDialog):
    def __init__(self, parent, schedule: Schedule, size: int, asset: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("업무메이트 설정")
        self.asset = asset
        layout = QFormLayout(self)
        self.size = QSpinBox()
        self.size.setRange(80, 420)
        self.size.setSuffix(" px")
        self.size.setValue(size)
        layout.addRow("캐릭터 크기", self.size)

        self.times = []
        for label, value in (
            ("출근", schedule.start), ("점심 시작", schedule.lunch_start),
            ("점심 종료", schedule.lunch_end), ("퇴근", schedule.end),
        ):
            field = QTimeEdit(QTime(value.hour, value.minute))
            field.setDisplayFormat("HH:mm")
            layout.addRow(label, field)
            self.times.append(field)

        self.asset_label = QLabel(asset.name)
        self.asset_label.setToolTip(str(asset))
        layout.addRow("현재 리소스", self.asset_label)
        choose_image = QPushButton("GIF 또는 PNG 파일 선택")
        choose_image.clicked.connect(self.choose_image)
        layout.addRow(choose_image)
        choose_frames = QPushButton("PNG 프레임 폴더 선택")
        choose_frames.clicked.connect(self.choose_frames)
        layout.addRow(choose_frames)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "캐릭터 파일 선택", str(self.asset.parent), "캐릭터 이미지 (*.gif *.png)"
        )
        if path:
            self.set_asset(Path(path))

    def choose_frames(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "PNG 프레임 폴더 선택", str(self.asset.parent))
        if path:
            self.set_asset(Path(path))

    def set_asset(self, path: Path) -> None:
        self.asset = path
        self.asset_label.setText(path.name)
        self.asset_label.setToolTip(str(path))

    @property
    def schedule(self) -> Schedule:
        return Schedule(*(time(field.time().hour(), field.time().minute()) for field in self.times))

    def accept(self) -> None:
        if not self.schedule.valid:
            QMessageBox.warning(self, "시간 설정 확인", "출근, 점심 시작, 점심 종료, 퇴근 순서로 설정하세요.")
            return
        super().accept()


class DesktopMate(QWidget):
    def __init__(self, settings: QSettings, assets: Path) -> None:
        super().__init__(
            None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowTitle("쿠리만쥬 업무메이트")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.settings = settings
        self.pack_directory = assets / "actions"
        self.schedule = self._read_schedule()
        self.character_size = max(80, min(420, settings.value("size", 180, type=int)))
        gif = assets / "character.gif"
        default_asset = assets / "frames" if not gif.exists() and (assets / "frames").is_dir() else gif
        self.asset = Path(settings.value("asset", str(default_asset), type=str))
        self.press_point: QPoint | None = None
        self.dragged = False
        self.asset_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.bubble = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.bubble.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.bubble.setTextFormat(Qt.TextFormat.PlainText)
        self.bubble.setWordWrap(True)
        self.bubble.setFixedWidth(230)
        self.bubble.setStyleSheet(
            "QLabel {background: #fff7e8; color: #432c24; border: 1px solid #d9ae8e;"
            "border-radius: 15px; padding: 9px; font-size: 14px;}"
        )
        layout.addWidget(self.bubble, alignment=Qt.AlignmentFlag.AlignHCenter)

        self.animation = Animation()
        self.player = ActionPlayer(self.animation.show_frame, self)
        self.player.failed.connect(self._show_pack_error)
        self.animation.unavailable.connect(self._asset_unavailable)
        self.animation.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.animation, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        self.reaction = QTimer(self, singleShot=True, interval=2000)
        self.reaction.timeout.connect(self._refresh)
        self.clock = QTimer(self, interval=250)
        self.clock.timeout.connect(self._refresh)
        self._load_asset()
        self.clock.start()
        layout.activate()
        self._restore_position()

    def _read_schedule(self) -> Schedule:
        defaults = Schedule()
        try:
            schedule = Schedule(*(time.fromisoformat(self.settings.value(
                f"schedule/{key}", getattr(defaults, key).isoformat(), type=str
            )) for key in ("start", "lunch_start", "lunch_end", "end")))
        except ValueError:
            return defaults
        return schedule if schedule.valid else defaults

    def _load_asset(self) -> None:
        self.asset_error = ""
        self.player.stop()
        self.animation.stop()
        self.animation.setFixedSize(self.character_size, self.character_size)
        self.animation.show()
        try:
            if (self.pack_directory / "pack.json").is_file():
                self.player.set_pack(Pack.read(self.pack_directory))
            else:
                self.animation.load(self.asset)
        except ValueError as error:
            self._asset_unavailable(f"동작 팩 오류: {error}")
        except OSError:
            self._asset_unavailable("캐릭터 경로에 접근할 수 없어요.")
        self._refresh()

    def _show_pack_error(self, message: str) -> None:
        self.asset_error = f"동작 팩 오류: {message}"
        self._refresh()

    def _asset_unavailable(self, message: str) -> None:
        self.asset_error = message
        self.animation.hide()
        self._refresh()

    def _refresh(self) -> None:
        countdown, status = self.schedule.display(datetime.now())
        if self.reaction.isActive():
            status = "조금만 더 힘내요!"
        lines = [countdown, status]
        if self.asset_error:
            lines.append(self.asset_error + "\n우클릭 → 설정")
        text = "\n".join(lines)
        if self.bubble.text() != text:
            self.bubble.setText(text)

    def _restore_position(self) -> None:
        position = self.settings.value("position", None)
        if isinstance(position, QPoint):
            screen = QApplication.screenAt(position + self.rect().center())
            if screen and screen.availableGeometry().contains(position + self.bubble.geometry().center()):
                self.move(position)
                return
        area = QApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 18, area.bottom() - self.height() - 18)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_point = event.globalPosition().toPoint()
            self.press_origin = self.pos()
            self.dragged = False
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.press_point is None:
            return
        offset = event.globalPosition().toPoint() - self.press_point
        if self.dragged or offset.manhattanLength() >= QApplication.startDragDistance():
            self.dragged = True
            self.move(self.press_origin + offset)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.press_point is None:
            return
        self.press_point = None
        if self.dragged:
            self.settings.setValue("position", self.pos())
        else:
            self.reaction.start()
            self._refresh()

    def closeEvent(self, event) -> None:
        self.clock.stop()
        self.player.stop()
        self.reaction.stop()
        self.animation.stop()
        super().closeEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        preferences = menu.addAction("설정")
        tests = menu.addMenu("애니메이션 테스트")
        previews = {}
        for name, label in ACTIONS.items():
            action = tests.addAction(label)
            action.setEnabled(self.player.pack is not None and name in self.player.pack.clips)
            previews[action] = name
        quit_action = menu.addAction("종료")
        selected = menu.exec(event.globalPos())
        if selected == preferences:
            self._edit_preferences()
        elif selected in previews:
            self.player.play(previews[selected])
        elif selected == quit_action:
            self.close()

    def _edit_preferences(self) -> None:
        dialog = Preferences(self, self.schedule, self.character_size, self.asset)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.schedule = dialog.schedule
        self.character_size = dialog.size.value()
        self.asset = dialog.asset
        self.settings.setValue("asset", str(self.asset))
        self.settings.setValue("size", self.character_size)
        for key in ("start", "lunch_start", "lunch_end", "end"):
            self.settings.setValue(f"schedule/{key}", getattr(self.schedule, key).isoformat())
        self._load_asset()
        self.layout().activate()
        screen = QApplication.screenAt(self.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(
            max(area.left(), min(self.x(), area.right() - self.width() + 1)),
            max(area.top(), min(self.y(), area.bottom() - self.height() + 1)),
        )
        self.settings.setValue("position", self.pos())
