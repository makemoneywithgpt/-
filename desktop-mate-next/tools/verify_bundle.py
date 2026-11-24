"""Check source animation and verify the Windows distribution layout."""

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from PySide6.QtGui import QGuiApplication, QImage, QMovie

from mate.clips import Pack

ASSETS = Path(__file__).resolve().parents[1] / "assets"


def sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_animation(assets: Path) -> dict:
    directory = assets / "actions"
    if (directory / "pack.json").is_file():
        pack = Pack.read(directory)
        summary = {}
        for action, frames in pack.clips.items():
            for frame in frames:
                pack.image(frame)
            summary[action] = {"frames": len(frames), "ms": sum(frame.ms for frame in frames)}
        return {"type": "pack", "canvas": pack.size, "clips": summary,
                "frames": sum(clip["frames"] for clip in summary.values())}
    gif = assets / "character.gif"
    if gif.is_file():
        movie = QMovie(str(gif), b"gif")
        frames = movie.frameCount()
        if not movie.isValid() or frames < 2:
            raise ValueError("assets/character.gif must be a readable GIF with at least two frames.")
        for index in range(frames):
            if not movie.jumpToFrame(index) or movie.currentPixmap().isNull():
                raise ValueError(f"Cannot decode GIF frame {index + 1}.")
        return {"type": "gif", "frames": frames}

    directory = assets / "frames"
    frames = [path for path in directory.iterdir()
              if path.is_file() and path.suffix.lower() == ".png"] if directory.is_dir() else []
    if not frames:
        return {"type": "missing", "frames": 0}
    if len(frames) < 2:
        raise ValueError("assets/frames needs at least two PNG files for animation.")
    for path in sorted(frames):
        image = QImage(str(path), "PNG")
        if image.isNull() or not image.hasAlphaChannel():
            raise ValueError(f"Expected a readable PNG with an alpha channel: {path.name}")
    return {"type": "png", "frames": len(frames)}


def asset_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): sha256(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def stage_and_verify(bundle: Path, animation: dict) -> None:
    required = (
        bundle / "KurimanjuMateNext.exe",
        bundle / "_internal/PySide6/plugins/platforms/qwindows.dll",
        bundle / "_internal/PySide6/plugins/imageformats/qgif.dll",
    )
    for path in required:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing bundle component: {path.relative_to(bundle)}")

    expected = asset_hashes(ASSETS)
    shipped = bundle / "assets"
    if not shipped.exists():
        shutil.copytree(ASSETS, shipped)
    if asset_hashes(shipped) != expected:
        raise ValueError("Bundled assets do not match the source SHA-256 hashes.")
    manifest = {
        "animation": animation,
        "asset_sha256": expected,
        "required_files": [path.relative_to(bundle).as_posix() for path in required],
        "settings": {"backend": "Windows registry", "key": r"HKCU\Software\DesktopMate\KurimanjuNext"},
    }
    (bundle / "bundle-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Verified Windows bundle layout and {len(expected)} asset file(s).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Stage and verify assets next to the built EXE.")
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication([])
    try:
        animation = check_animation(ASSETS)
        if animation["type"] == "missing":
            print("WARNING: No character animation supplied. Add assets/character.gif or assets/frames.")
        else:
            print(f"Decoded {animation['frames']} {animation['type'].upper()} animation frames.")
        if args.bundle:
            stage_and_verify(args.bundle, animation)
    except (OSError, ValueError) as error:
        print(f"Verification failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
