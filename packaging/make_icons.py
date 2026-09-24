#!/usr/bin/env python3
"""Generate deterministic Argus application icons from the repository logo geometry."""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

BRAND = (20, 201, 172, 255)


def artwork(size: int = 1024) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    c = size // 2
    scale = size / 28.0

    def px(value: float) -> int:
        return max(1, round(value * scale))

    draw.ellipse(
        (c - px(13), c - px(13), c + px(13), c + px(13)),
        outline=BRAND,
        width=px(2),
    )
    draw.ellipse(
        (c - px(6), c - px(6), c + px(6), c + px(6)),
        fill=(20, 201, 172, 77),
    )
    draw.ellipse(
        (c - px(3), c - px(3), c + px(3), c + px(3)),
        fill=BRAND,
    )
    for x, y in ((14, 5), (14, 23), (5, 14), (23, 14)):
        cx, cy = px(x), px(y)
        r = px(2)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(20, 201, 172, 179))
    return image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image = artwork()
    suffix = args.output.suffix.lower()
    if suffix == ".png":
        image.save(args.output, format="PNG")
    elif suffix == ".ico":
        image.save(
            args.output,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
    elif suffix == ".icns":
        image.save(args.output, format="ICNS")
    else:
        raise SystemExit("output must end in .png, .ico, or .icns")


if __name__ == "__main__":
    main()
