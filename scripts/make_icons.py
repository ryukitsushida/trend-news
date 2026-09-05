"""PWAアイコン(icon-192.png / icon-512.png)をローカルで1回だけ生成するスクリプト。

CI では実行しない(Pillow を requirements.txt に含めない)。
使い方: pip install pillow && python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_DIR = Path(__file__).resolve().parent.parent / "static"
BG_COLOR = (15, 23, 42)  # --bg dark
FG_COLOR = (96, 165, 250)  # --accent dark
LABEL = "TN"


def make_icon(size: int, path: Path) -> None:
    img = Image.new("RGB", (size, size), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font = None
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ):
        if Path(candidate).exists():
            try:
                font = ImageFont.truetype(candidate, size=int(size * 0.42))
                break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), LABEL, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]),
        LABEL,
        fill=FG_COLOR,
        font=font,
    )
    img.save(path, "PNG")
    print(f"wrote {path}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    make_icon(192, OUT_DIR / "icon-192.png")
    make_icon(512, OUT_DIR / "icon-512.png")


if __name__ == "__main__":
    main()
