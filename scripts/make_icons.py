"""The page's icons, made from docs/logo.jpg: `python3 scripts/make_icons.py` (needs Pillow; run by hand when
the logo changes, the results are committed).

Two pictures come out of the logo. The favicon is the canary by itself on nothing: cut out by its colour,
saturated yellow holds at 16 pixels on a dark tab bar and a light one, where the logo's pale tile is a grey
smudge. The header, and the icons a phone puts on its home screen, have room for the whole tile: the
squircle with the canary on its platter, with nothing behind its rounded corners.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "drivecanary" / "web" / "static"

#: where things are in docs/logo.jpg (1408 x 768), read off a ruler laid over it
LOGO_SIZE = (1408, 768)
TILE = (462, 94, 944, 578)  # the squircle, just inside its edge
TILE_RADIUS = 0.23  # of its width
CANARY = (692, 100, 972, 400)  # a box with the whole bird in it
INSIDE = (130, 150)  # a point of that box that is certainly bird
HEAD_ENDS = 140  # above this row of the box, holes in the yellow are eyes and beak, and are filled


def cut_out_canary(logo: Image.Image) -> Image.Image:
    crop = logo.crop(CANARY)
    px = crop.load()
    width, height = crop.size
    yellow = Image.new("L", crop.size, 0)
    mark = yellow.load()
    for y in range(height):
        for x in range(width):
            r, g, b = px[x, y]
            if r > 150 and g > 100 and b < 0.62 * g and r - b > 95:  # saturated yellow and orange, nothing paler
                mark[x, y] = 255
    # closed over: the eyes and the glint on the beak are holes in the yellow
    closed = yellow.filter(ImageFilter.MaxFilter(23)).filter(ImageFilter.MinFilter(23))
    closed = closed.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))
    ImageDraw.floodfill(closed, INSIDE, 128, thresh=10)  # the one piece that the bird is; specks elsewhere go
    body = closed.point(lambda v: 255 if v == 128 else 0)
    # below the head the closing must not fill anything in: between the legs is platter, not bird
    near_yellow = yellow.filter(ImageFilter.MaxFilter(3))
    keep = Image.new("L", crop.size, 0)
    keep.paste(body.crop((0, 0, width, HEAD_ENDS)), (0, 0))
    lower = Image.composite(body, Image.new("L", crop.size, 0), near_yellow).crop((0, HEAD_ENDS, width, height))
    keep.paste(lower, (0, HEAD_ENDS))
    out = crop.convert("RGBA")
    out.putalpha(keep.filter(ImageFilter.GaussianBlur(1.0)))
    return out.crop(keep.getbbox())


def centred(canary: Image.Image, size: int, pad: float) -> Image.Image:
    """The canary in a transparent square, drawn at eight times the size and brought down."""
    big = size * 8
    square = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    room = int(big * (1 - 2 * pad))
    scale = min(room / canary.size[0], room / canary.size[1])
    bird = canary.resize((int(canary.size[0] * scale), int(canary.size[1] * scale)), Image.LANCZOS)
    square.alpha_composite(bird, ((big - bird.size[0]) // 2, (big - bird.size[1]) // 2))
    return square.resize((size, size), Image.LANCZOS)


def tile(logo: Image.Image, size: int, *, rounded: bool) -> Image.Image:
    """The logo's squircle. Rounded, there is nothing behind its corners; square, for a phone that rounds
    an icon itself and paints black wherever it finds transparency."""
    left, top, right, bottom = TILE
    side = min(right - left, bottom - top)
    out = logo.crop((left, top, left + side, top + side)).convert("RGBA")
    if rounded:
        mask = Image.new("L", (side * 4, side * 4), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, side * 4 - 1, side * 4 - 1), radius=int(side * 4 * TILE_RADIUS), fill=255
        )
        out.putalpha(mask.resize((side, side), Image.LANCZOS))
    return out.resize((size, size), Image.LANCZOS)


def main() -> None:
    logo = Image.open(ROOT / "docs" / "logo.jpg").convert("RGB")
    if logo.size != LOGO_SIZE:
        raise SystemExit(f"docs/logo.jpg is {logo.size}, not {LOGO_SIZE}: measure CANARY, INSIDE and HEAD_ENDS again")
    canary = cut_out_canary(logo)
    made = {
        "favicon-16.png": centred(canary, 16, 0.0),  # no room to spare at this size
        "favicon-32.png": centred(canary, 32, 0.02),
        "icon-192.png": tile(logo, 192, rounded=True),
        "apple-touch-icon.png": tile(logo, 180, rounded=False),
        "logo-tile.png": tile(logo, 88, rounded=True),  # the header's, at twice the size it is shown
    }
    for name, image in made.items():
        image.save(STATIC / name, optimize=True)
        print(f"{name}: {image.size[0]}x{image.size[1]}, {(STATIC / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
