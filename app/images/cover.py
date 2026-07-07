"""SKU-master card cover generation.

Composites a pet PNG onto a rarity-colored background (StarPets-style concentric white
rings + rarity-tinted radial + sparkles) with a pumping-type badge in the top-right corner.

Config-driven: tweak RARITY_COLORS / _BADGE and re-run /regenerate-covers to restyle every
card — the card structure (variants, mapping) is untouched, only the cover image changes.
"""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SZ = 512

# rare value -> (inner, outer, sparkle) RGB. Unknown rarities fall back to "common".
RARITY_COLORS = {
    "common":     ((228, 231, 236), (198, 203, 212), (150, 155, 165)),
    "uncommon":   ((198, 242, 186), (120, 214, 104), (70, 180, 60)),
    "rare":       ((205, 219, 255), (120, 150, 246), (60, 110, 240)),
    "ultra_rare": ((246, 206, 250), (226, 150, 240), (200, 70, 220)),
    "legendary":  ((255, 236, 200), (255, 203, 120), (240, 150, 30)),
}
# pumping value -> badge colour. Values not here (None / non-pet) get no badge.
_BADGE = {"default": (150, 155, 165), "neon": (88, 196, 45), "mega_neon": (150, 60, 220)}


def _font(sz):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", sz)
    except Exception:
        return ImageFont.load_default()


def _sparkle(d, cx, cy, r, col):
    d.polygon([
        (cx, cy - r), (cx + r * 0.28, cy - r * 0.28), (cx + r, cy), (cx + r * 0.28, cy + r * 0.28),
        (cx, cy + r), (cx - r * 0.28, cy + r * 0.28), (cx - r, cy), (cx - r * 0.28, cy - r * 0.28),
    ], fill=col)


def _background(rare: str) -> Image.Image:
    inner, outer, spark = RARITY_COLORS.get((rare or "").lower(), RARITY_COLORS["common"])
    img = Image.new("RGBA", (SZ, SZ), (247, 248, 250, 255))
    grad = Image.new("RGBA", (SZ, SZ), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    cx = cy = SZ // 2
    maxr = int(SZ * 0.42)
    for r in range(maxr, 0, -1):
        t = r / maxr
        col = tuple(int(inner[i] + (outer[i] - inner[i]) * t) for i in range(3))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col + (255,))
    grad = grad.filter(ImageFilter.GaussianBlur(6))
    img.alpha_composite(grad)
    d = ImageDraw.Draw(img)
    for rr in (int(SZ * 0.40), int(SZ * 0.33)):
        d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=(255, 255, 255, 255), width=12)
    for (sx, sy, sr) in ((0.80, 0.22, 20), (0.88, 0.33, 13), (0.15, 0.82, 17)):
        _sparkle(d, int(SZ * sx), int(SZ * sy), sr, spark + (255,))
    return img


def _badge(img: Image.Image, pumping: str) -> Image.Image:
    kind = (pumping or "").lower()
    if kind not in _BADGE:
        return img
    d = ImageDraw.Draw(img)
    r = 46
    cx = SZ - r - 22
    cy = r + 22
    d.rounded_rectangle([cx - r, cy - r, cx + r, cy + r], radius=20,
                        fill=_BADGE[kind] + (255,), outline=(255, 255, 255, 255), width=6)
    if kind == "default":
        bw, bh = int(r * 0.5), max(4, int(r * 0.13))
        d.rounded_rectangle([cx - bw, cy - bh, cx + bw, cy + bh], radius=bh, fill=(255, 255, 255, 255))
    else:
        sym = "N" if kind == "neon" else "M"
        f = _font(52)
        tb = d.textbbox((0, 0), sym, font=f)
        d.text((cx - (tb[2] - tb[0]) / 2 - tb[0], cy - (tb[3] - tb[1]) / 2 - tb[1]),
               sym, font=f, fill=(255, 255, 255, 255))
    return img


def make_cover(pet_png: bytes, rare: str, pumping: str) -> bytes:
    """Return PNG bytes: pet centered on a rarity background with a pumping badge."""
    bg = _background(rare)
    if pet_png:
        try:
            pet = Image.open(BytesIO(pet_png)).convert("RGBA")
            # StarPets source is ~110px, so we UPSCALE (resize, not thumbnail) to fill the ring
            box = int(SZ * 0.62)
            scale = min(box / pet.width, box / pet.height)
            pet = pet.resize(
                (max(1, int(pet.width * scale)), max(1, int(pet.height * scale))),
                Image.LANCZOS,
            )
            # 110px source -> upscaled ~3x is soft; unsharp restores edge crispness.
            # Sharpen only the RGB, keep alpha intact so the halo isn't fringed.
            r, g, b, a = pet.split()
            rgb = Image.merge("RGB", (r, g, b)).filter(
                ImageFilter.UnsharpMask(radius=2.0, percent=110, threshold=2)
            )
            pet = Image.merge("RGBA", (*rgb.split(), a))
            bg.alpha_composite(pet, ((SZ - pet.width) // 2, (SZ - pet.height) // 2))
        except Exception as e:
            print(f"[cover] pet composite failed: {e}", flush=True)
    _badge(bg, pumping)
    out = BytesIO()
    bg.convert("RGBA").save(out, format="PNG")
    return out.getvalue()
