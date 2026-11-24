from random import randint

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap

from mate.clips import Pack


class ActionPlayer(QObject):
    failed = Signal(str)

    def __init__(self, display, parent=None):
        super().__init__(parent)
        self.display = display
        self.pack: Pack | None = None
        self.action = ""
        self.index = 0
        self.frames: list[QPixmap] = []
        self.idle: list[QPixmap] = []
        self.special_index = 0
        self.frame_timer = QTimer(self, singleShot=True, timerType=Qt.TimerType.PreciseTimer)
        self.frame_timer.timeout.connect(self.advance)
        self.special_timer = QTimer(self, singleShot=True)
        self.special_timer.timeout.connect(self.play_next_special)

    def set_pack(self, pack: Pack):
        idle = [QPixmap.fromImage(pack.image(frame)) for frame in pack.clips["idle"]]
        self.stop()
        self.pack, self.idle = pack, idle
        self.play("idle")

    def stop(self):
        self.frame_timer.stop()
        self.special_timer.stop()
        self.pack = None
        self.frames = []
        self.idle = []
        self.action = ""
        self.special_index = 0

    def play(self, action: str):
        if self.pack is None or action not in self.pack.clips:
            return
        try:
            frames = self.idle if action == "idle" else [
                QPixmap.fromImage(self.pack.image(frame)) for frame in self.pack.clips[action]
            ]
        except ValueError as error:
            self.play("idle")
            self.failed.emit(str(error))
            return
        self.frames, self.action, self.index = frames, action, 0
        self.special_timer.stop()
        if action == "idle" and len(self.pack.clips) > 1:
            self.special_timer.start(randint(90_000, 180_000))
        self.show_current()

    def show_current(self):
        self.display(self.frames[self.index])
        if self.action != "idle" or len(self.frames) > 1:
            self.frame_timer.start(self.pack.clips[self.action][self.index].ms)
        else:
            self.frame_timer.stop()

    def advance(self):
        self.index += 1
        if self.index == len(self.frames):
            if self.action != "idle":
                self.play("idle")
                return
            self.index = 0
        self.show_current()

    def play_next_special(self):
        actions = [action for action in self.pack.clips if action != "idle"]
        self.play(actions[self.special_index % len(actions)])
        self.special_index += 1
