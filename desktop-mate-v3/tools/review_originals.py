"""Prepare the reviewed work clip and generate source/crop/alpha review artifacts.

Run with Pillow, NumPy and OpenCV. Source GIFs are read-only.
"""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageSequence


NAMES = {
    "work_pull": "쿠리만쥬 풀뽑기.gif",
    "drink_can": "쿠리만쥬 술먹방2.gif",
    "lemon_drink": "쿠리만쥬 술먹방.gif",
    "hangover_soup": "쿠리만쥬 해장국.gif",
}
CROPS = {
    "work_pull": (144, 36, 354, 262),
    "drink_can": (155, 0, 490, 334),
    "lemon_drink": (0, 34, 512, 512),
    "hangover_soup": (0, 0, 640, 350),
}
WORK_SHA = "32ae2049f6b1fcbd9c7fdfa8677439e7676ed0c177bf1ccba28ade759253a597"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def work_mask(frame, index):
    pixels = np.array(frame)
    red, green, blue = pixels.astype(np.int16).transpose(2, 0, 1)
    y, x = np.indices(red.shape)
    roi = (x >= 153) & (x < 349) & (y >= 40) & (y < 256)
    roi &= ((y < 211) | (x >= 165)) & ((y < 232) | (x >= 174))
    warm = (red - blue > 17) & (red - green > 5)
    ink = (red < 110) & (red - green > 8) & (red - blue > 4)
    binary = ((warm | ink) & roi).astype(np.uint8)

    # The hand raises the plant around frame 26. Use different reviewed
    # contour limits before and after that moment to exclude the surrounding grass.
    shift = 0 if index <= 25 else 8 if index <= 32 else 5
    right = np.interp(y, [85, 100, 113, 125, 143, 157, 172, 184, 198, 211, 223, 255],
                      [308, 329, 325, 330, 332, 331, 327, 326, 329, 323, 318, 292]) + shift
    left = np.interp(y, [84, 98, 108, 120, 139, 156, 170, 185, 195, 211, 224, 240, 255],
                     [171, 158, 161, 156, 152, 152, 155, 158, 163, 163, 168, 183, 207])
    if index > 28:
        left += shift
    if index >= 28:
        stricter = np.interp(y, [139, 155, 167, 179, 187, 197, 207],
                             [161, 166, 170, 176, 178, 173, 171])
        left = np.maximum(left, np.where((y >= 139) & (y <= 207), stricter, 0))
    binary[(x > right + 2) | (x < left - 2)] = 0
    if index > 25:
        binary[(y > 206) & (x < 179)] = 0
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    solid = np.zeros_like(binary)
    cv2.drawContours(solid, [max(contours, key=cv2.contourArea)], 0, 255, cv2.FILLED)
    opened = cv2.morphologyEx(solid, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    hand_y = (193, 239) if index < 26 else (140, 214)
    hand = (y >= hand_y[0]) & (y <= hand_y[1]) & (x < (210 if index < 26 else 214))
    return np.where(hand, solid, opened)


def work_cutouts(frames):
    area = CROPS["work_pull"]
    left, top, right, bottom = area
    masks = [work_mask(frame, index)[top:bottom, left:right]
             for index, frame in enumerate(frames)]
    source = [np.array(frame.crop(area)) for frame in frames]
    gray = [cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY) for rgb in source]
    yy, xx = np.indices(masks[0].shape, dtype=np.float32)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    crop_x, crop_y = xx + left, yy + top
    result = []
    for index, (mask, pixels) in enumerate(zip(masks, source)):
        if 0 < index < len(masks) - 1:
            neighbors = [mask]
            for neighbor in (index - 1, index + 1):
                flow = cv2.calcOpticalFlowFarneback(gray[index], gray[neighbor], None,
                                                    0.5, 3, 15, 3, 5, 1.2, 0)
                warped = cv2.remap(masks[neighbor], xx + flow[..., 0], yy + flow[..., 1],
                                   cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
                neighbors.append(warped)
            vote = np.median(neighbors, axis=0).astype(np.uint8)
            boundary = cv2.dilate(mask, kernel) != cv2.erode(mask, kernel)
            hand = (crop_x < 214) & (crop_y >= 140) & (crop_y <= 239)
            mask = np.where(boundary & ~hand, vote, mask)
        alpha = cv2.GaussianBlur(mask, (3, 3), 0.55)
        masked = pixels.copy()
        masked[alpha == 0] = 0
        result.append(Image.fromarray(np.dstack((masked, alpha))))
    return result


def composite(image, size, color):
    tile = Image.new("RGB", size, color)
    image = image.convert("RGBA")
    image.thumbnail(size, Image.Resampling.LANCZOS)
    tile.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2), image)
    return tile


def review_sheet(path, key, frames, durations, cutouts):
    columns = 3 if cutouts else 2
    headings = ["Original", "Fixed crop", "Transparent cutout"]
    indices = [round((len(frames) - 1) * n / 7) for n in range(8)]
    sheet = Image.new("RGB", (224 * columns, 8 * 174), "#e9e6df")
    draw = ImageDraw.Draw(sheet)
    for row, index in enumerate(indices):
        original = frames[index]
        variants = [original, original.crop(CROPS[key])]
        if cutouts:
            variants.append(cutouts[index])
        for column, frame in enumerate(variants):
            x, y = column * 224, row * 174
            color = "#343434" if row % 2 else "#f9f9f9"
            sheet.paste(composite(frame, (220, 149), color), (x + 2, y + 21))
            draw.text((x + 5, y + 4), f"{headings[column]} #{index+1} / {durations[index]} ms", fill="#222")
    sheet.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("originals", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir()
    pack = args.output / "actions"
    (pack / "work_pull").mkdir(parents=True)
    reviews = args.output / "review"
    reviews.mkdir()
    reports = {}
    for key, filename in NAMES.items():
        path = args.originals / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with Image.open(path) as source:
            size, loop = source.size, source.info.get("loop")
            frames, durations = [], []
            for frame in ImageSequence.Iterator(source):
                frames.append(frame.convert("RGB"))
                durations.append(frame.info["duration"])
        cutouts = []
        report = {
            "source": filename, "sha256": digest, "size": size, "loop": loop,
            "frame_count": len(frames), "total_ms": sum(durations), "frame_ms": durations,
            "status": "held",
        }
        if key == "work_pull":
            if digest != WORK_SHA or size != (498, 281) or len(frames) != 50:
                raise ValueError("The reviewed masks are specific to the uploaded work GIF.")
            cutouts = work_cutouts(frames)
            clip, bounds = [], []
            for index, (frame, ms) in enumerate(zip(cutouts, durations), 1):
                name = f"work_pull/{index:04}.png"
                box = frame.getchannel("A").getbbox()
                if not box or min(box[:2]) < 1 or box[2] >= frame.width or box[3] >= frame.height:
                    raise ValueError(f"Missing alpha padding at frame {index}")
                frame.save(pack / name)
                clip.append({"file": name, "ms": ms})
                bounds.append(box)
            write_json(pack / "pack.json", {
                "canvas": list(cutouts[0].size),
                "clips": {"work_pull": clip},
            })
            report.update(status="applied", bounds=bounds, output_frame_ms=durations)
            movies = []
            for raw, cut in zip(frames, cutouts):
                pair = Image.new("RGB", (620, 320), "#eee")
                pair.paste(composite(raw, (310, 280), "#fff"), (0, 29))
                pair.paste(composite(cut, (310, 280), "#393939"), (310, 29))
                titles = ImageDraw.Draw(pair)
                titles.text((8, 8), "Original", fill="#222")
                titles.text((318, 8), "Fixed canvas / alpha", fill="#222")
                movies.append(pair)
            movies[0].save(reviews / "work_pull-comparison.gif", save_all=True,
                           append_images=movies[1:], duration=durations, loop=0, disposal=2)
        review_sheet(reviews / f"{key}-comparison.png", key, frames, durations, cutouts)
        reports[key] = report
    write_json(reviews / "source-timing.json", reports)


if __name__ == "__main__":
    main()
