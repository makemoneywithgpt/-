"""Read and validate original-frame animation packs."""

import json
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage


ACTIONS = {"idle": "기본 대기", "drink": "음료 마시기", "doze": "졸기"}


@dataclass(frozen=True)
class Frame:
    path: Path
    ms: int


@dataclass(frozen=True)
class Pack:
    size: tuple[int, int]
    clips: dict[str, tuple[Frame, ...]]

    @classmethod
    def read(cls, directory: Path):
        data = json.loads((directory / "pack.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("pack.json must be a JSON object")
        size = data.get("canvas")
        if (not isinstance(size, list) or len(size) != 2
                or any(type(edge) is not int or not 1 <= edge <= 4096 for edge in size)):
            raise ValueError("canvas must contain two positive dimensions up to 4096")
        clips = data.get("clips")
        if not isinstance(clips, dict) or "idle" not in clips or clips.keys() - ACTIONS.keys():
            raise ValueError("clips must contain idle and may contain drink and doze")
        root = directory.resolve()
        parsed = {}
        for action, entries in clips.items():
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{action}: frame list is empty or invalid")
            frames = []
            for index, entry in enumerate(entries, start=1):
                if not isinstance(entry, dict):
                    raise ValueError(f"{action} frame {index}: expected an object")
                file, ms = entry.get("file"), entry.get("ms")
                if not isinstance(file, str) or type(ms) is not int or not 1 <= ms <= 2_147_483_647:
                    raise ValueError(f"{action} frame {index}: expected file and positive milliseconds")
                path = (root / file).resolve()
                if not path.is_relative_to(root) or path.suffix.lower() != ".png" or not path.is_file():
                    raise ValueError(f"{action} frame {index}: missing or invalid PNG: {file}")
                frames.append(Frame(path, ms))
            parsed[action] = tuple(frames)
        return cls(tuple(size), parsed)

    def image(self, frame: Frame) -> QImage:
        image = QImage(str(frame.path), "PNG")
        if image.isNull() or (image.width(), image.height()) != self.size or not image.hasAlphaChannel():
            raise ValueError(f"Expected an RGBA PNG on canvas {self.size}: {frame.path.name}")
        width, height = self.size
        if (any(image.pixelColor(x, y).alpha() for x in range(width) for y in (0, height - 1))
                or any(image.pixelColor(x, y).alpha() for y in range(height) for x in (0, width - 1))):
            raise ValueError(f"Frame needs transparent padding on every edge: {frame.path.name}")
        return image
