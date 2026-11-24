"""Extract original GIF frames or align manually matted source frames."""

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageChops, ImageSequence

from mate.clips import ACTIONS


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract(source: Path, output: Path, untimed_ms: int | None) -> None:
    frames = []
    with Image.open(source) as gif:
        if gif.format != "GIF":
            raise ValueError("Expected an original GIF file")
        for index, frame in enumerate(ImageSequence.Iterator(gif), start=1):
            ms = frame.info.get("duration", 0) or untimed_ms
            if type(ms) is not int or ms < 1:
                raise ValueError(f"Frame {index} has no positive duration; specify --untimed-ms explicitly.")
            name = f"{index:04}.png"
            frame.convert("RGBA").save(output / name)
            frames.append({"file": name, "ms": ms})
    write_json(output / "frames.json", frames)


def assemble(recipe: Path, output: Path) -> None:
    data = json.loads(recipe.read_text(encoding="utf-8"))
    width, height = data["canvas"]
    if any(type(edge) is not int or not 1 <= edge <= 4096 for edge in (width, height)):
        raise ValueError("canvas dimensions must be integers from 1 to 4096")
    clips = data["clips"]
    if "idle" not in clips or clips.keys() - ACTIONS.keys():
        raise ValueError("Provide idle and only supported clips")
    definitions, report = {}, {}
    for action, frames in clips.items():
        if not frames:
            raise ValueError(f"{action}: empty clip")
        (output / action).mkdir()
        definitions[action], report[action] = [], []
        for index, entry in enumerate(frames, start=1):
            name = f"{action}/{index:04}.png"
            ms = entry["ms"]
            x, y = entry.get("offset", [0, 0])
            if type(ms) is not int or not 1 <= ms <= 2_147_483_647 or any(type(v) is not int for v in (x, y)):
                raise ValueError(f"{name}: invalid duration or offset")
            with Image.open(recipe.parent / entry["file"]) as source:
                frame = source.convert("RGBA")
            alpha = frame.getchannel("A")
            if entry.get("mask"):
                with Image.open(recipe.parent / entry["mask"]) as source:
                    if source.mode != "L" or source.size != frame.size:
                        raise ValueError(f"{name}: use a source-sized grayscale mask")
                    alpha = ImageChops.multiply(alpha, source)
            elif alpha.getextrema()[0] == 255:
                raise ValueError(f"{name}: opaque source needs a manually reviewed mask")
            frame.putalpha(alpha)
            box = alpha.getbbox()
            if not box:
                raise ValueError(f"{name}: empty foreground")
            left, top, right, bottom = box
            placed = (left + x, top + y, right + x, bottom + y)
            if not (0 < placed[0] < placed[2] < width and 0 < placed[1] < placed[3] < height):
                raise ValueError(f"{name}: foreground must have transparent padding on every edge")
            canvas = Image.new("RGBA", (width, height))
            canvas.paste(frame.crop(box), placed[:2])
            canvas.save(output / name)
            definitions[action].append({"file": name, "ms": ms})
            report[action].append({"file": name, "ms": ms, "bounds": placed,
                                   "size": [right - left, bottom - top]})
    write_json(output / "pack.json", {"canvas": [width, height], "clips": definitions})
    write_json(output / "alignment-report.json", report)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    extract_args = commands.add_parser("extract")
    extract_args.add_argument("source", type=Path)
    extract_args.add_argument("output", type=Path)
    extract_args.add_argument("--untimed-ms", type=int)
    assemble_args = commands.add_parser("assemble")
    assemble_args.add_argument("recipe", type=Path)
    assemble_args.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        output = args.output.resolve()
        if output.exists():
            raise ValueError(f"Output already exists: {output}")
        with TemporaryDirectory(dir=output.parent) as temporary:
            staged = Path(temporary) / "output"
            staged.mkdir()
            if args.command == "extract":
                extract(args.source, staged, args.untimed_ms)
            else:
                assemble(args.recipe, staged)
            staged.rename(output)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
