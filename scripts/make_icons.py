"""The page's icons, cut from docs/logo.jpg: `python3 scripts/make_icons.py` (needs Pillow; run by hand when
the logo changes, the results are committed).

At 32 pixels and up the icon is the whole tile (the rounded square with the canary on the platter); at 16 the
platter turns to mush, so the smallest one is the canary alone.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "drivecanary" / "web" / "static"


def rounded(im: Image.Image, radius: float) -> Image.Image:
    side = im.size[0]
    mask = Image.new("L", (side * 4, side * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side * 4 - 1, side * 4 - 1), radius=int(side * 4 * radius), fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask.resize((side, side), Image.LANCZOS))
    return out


#: where things are in docs/logo.jpg (1408 x 768), read off a ruler laid over it. Finding the tile by where
#: the picture stops being background does not work: its pale top edge is nearly the background's colour.
LOGO_SIZE = (1408, 768)
TILE = (458, 90, 948, 582)
CANARY = (692, 120, 972, 400)


def main() -> None:
    logo = Image.open(ROOT / "docs" / "logo.jpg").convert("RGB")
    if logo.size != LOGO_SIZE:
        raise SystemExit(f"docs/logo.jpg is {logo.size}, not {LOGO_SIZE}: measure TILE and CANARY again")
    tile = rounded(logo.crop(TILE).resize((490, 490), Image.LANCZOS), 0.2)
    bird = rounded(logo.crop(CANARY), 0.2)
    for name, source, size in (
        ("favicon-16.png", bird, 16),
        ("favicon-32.png", tile, 32),
        ("icon-64.png", tile, 64),
        ("apple-touch-icon.png", tile, 180),
        ("icon-192.png", tile, 192),
    ):
        source.resize((size, size), Image.LANCZOS).save(STATIC / name, optimize=True)
        print(f"{name}: {(STATIC / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
