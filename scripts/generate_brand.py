"""Generate HA-AgentHub brand assets in the current dashboard palette.

Palette (container/app/dashboard/static/css/tokens.css):
    bg:   #1a1816 -> #0d0c0a (warm near-black)
    hub:  #f5c26b -> #e2a84b (amber glow -> amber)

Outputs:
    container/app/dashboard/static/favicon.png   256x256
    custom_components/ha_agenthub/brand/icon.png 256x256
    custom_components/ha_agenthub/brand/logo.png 512x256

The SVG favicon is maintained by hand next to this script's geometry.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

BG_TOP = (26, 24, 22)  # #1a1816
BG_BOTTOM = (13, 12, 10)  # #0d0c0a
AMBER_GLOW = (245, 194, 107)  # #f5c26b
AMBER = (226, 168, 75)  # #e2a84b
TEXT_LIGHT = (245, 242, 238)  # #f5f2ee

# Hub geometry in a 24x24 viewbox (kept in sync with favicon.svg).
# The mark is one solid piece: connectors run from the center ring's stroke
# centerline into the filled node discs, so there are no gaps between parts.
CORNER_R = 5.5
STROKE = 1.8
CENTER = (12.0, 11.8, 3.6)
NODES = [(4.2, 5.4, 1.9), (19.8, 5.4, 1.9), (12.0, 20.4, 1.9)]


def _connectors() -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Lines from the center ring's stroke centerline to each node center."""
    cx, cy, cr = CENTER
    lines = []
    for nx, ny, _ in NODES:
        dx, dy = nx - cx, ny - cy
        dist = (dx * dx + dy * dy) ** 0.5
        lines.append(((cx + cr * dx / dist, cy + cr * dy / dist), (nx, ny)))
    return lines


def _lerp(a: tuple[int, ...], b: tuple[int, ...], t: float) -> tuple[int, ...]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def _vertical_gradient(
    height: int, top: tuple[int, ...], bottom: tuple[int, ...], width: int | None = None
) -> Image.Image:
    grad = Image.new("RGB", (1, height))
    for y in range(height):
        grad.putpixel((0, y), _lerp(top, bottom, y / (height - 1)))
    return grad.resize((width or height, height))


def render_icon(px: int) -> Image.Image:
    ss = 4  # supersampling factor
    s = px * ss
    u = s / 24  # pixels per viewbox unit

    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Background: warm near-black gradient inside a rounded square
    bg = _vertical_gradient(s, BG_TOP, BG_BOTTOM).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=CORNER_R * u, fill=255)
    img.paste(bg, (0, 0), mask)

    # Soft amber glow behind the hub
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    glow_r = 9.0 * u
    cx, cy = 12.0 * u, 10.8 * u
    steps = 60
    for i in range(steps, 0, -1):
        r = glow_r * i / steps
        alpha = round(56 * (1 - i / steps) ** 1.5)
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*AMBER, alpha))
    img.alpha_composite(glow)

    # Hub mark (one solid piece) on a mask, filled with an amber gradient:
    # connectors + filled node discs + filled center disc, then punch the
    # center hole so the ring interior shows the background again.
    hub_mask = Image.new("L", (s, s), 0)
    hd = ImageDraw.Draw(hub_mask)
    w = STROKE * u
    for (x1, y1), (x2, y2) in _connectors():
        hd.line([x1 * u, y1 * u, x2 * u, y2 * u], fill=255, width=round(w))
    for x, y, r in [CENTER, *NODES]:
        hd.ellipse([(x - r) * u, (y - r) * u, (x + r) * u, (y + r) * u], fill=255)
    cx, cy, cr = CENTER
    hole = cr - STROKE  # inner edge of the center ring stroke
    hd.ellipse([(cx - hole) * u, (cy - hole) * u, (cx + hole) * u, (cy + hole) * u], fill=0)
    hub_grad = _vertical_gradient(s, AMBER_GLOW, AMBER).convert("RGBA")
    img.paste(hub_grad, (0, 0), Image.composite(hub_mask, Image.new("L", (s, s), 0), mask))

    return img.resize((px, px), Image.LANCZOS)


def render_logo() -> Image.Image:
    w, h = 640, 256
    icon_px = 176
    logo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bg = _vertical_gradient(h, BG_TOP, BG_BOTTOM, width=w).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=28, fill=255)
    logo.paste(bg, (0, 0), mask)

    # Compose from the checked-in brand icon, do not re-render it
    icon = Image.open(ROOT / "custom_components/ha_agenthub/brand/icon.png").convert("RGBA")
    icon = icon.resize((icon_px, icon_px), Image.LANCZOS)
    icon_x, icon_y = 32, (h - icon_px) // 2
    logo.alpha_composite(icon, (icon_x, icon_y))

    d = ImageDraw.Draw(logo)
    text_x = icon_x + icon_px + 32
    parts = [("HA-Agent", TEXT_LIGHT), ("Hub", AMBER)]
    max_w = w - text_x - 32
    size = 64
    while size > 20:
        font = ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf", size)
        widths = [d.textlength(p, font=font) for p, _ in parts]
        if sum(widths) <= max_w:
            break
        size -= 2
    ascent, descent = font.getmetrics()
    text_y = (h - (ascent + descent)) // 2
    for (part, color), part_w in zip(parts, widths, strict=True):
        d.text((text_x, text_y), part, font=font, fill=(*color, 255))
        text_x += part_w
    return logo


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "logo":
        # Logo only: compose from the existing brand/icon.png
        render_logo().save(ROOT / "custom_components/ha_agenthub/brand/logo.png")
        print("brand/logo.png generated")
        return
    icon = render_icon(256)
    icon.save(ROOT / "container/app/dashboard/static/favicon.png")
    icon.save(ROOT / "custom_components/ha_agenthub/brand/icon.png")
    render_logo().save(ROOT / "custom_components/ha_agenthub/brand/logo.png")
    print("favicon.png, brand/icon.png, brand/logo.png generated")


if __name__ == "__main__":
    main()
