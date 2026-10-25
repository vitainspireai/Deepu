"""Small, memory-safe image helpers.

The server has 512 MB of RAM and an iPhone photo can take 150-300 MB once fully
decoded, so nothing here decodes a photo at full size: JPEGs are decoded at a
reduced scale ("draft" mode) and only the small result is processed.

Original photos are stored exactly as uploaded. Phones save portrait shots
sideways plus an EXIF "Orientation" flag; browsers honour that flag when showing
the original, so only the thumbnails we generate need rotating.
"""
import threading

from PIL import Image, ImageOps

THUMB_SIZE = (480, 480)
_thumb_slots = threading.BoundedSemaphore(2)   # an album page asks for dozens at once


def open_small(path, size, mode=None):
    """Open a photo decoded no bigger than needed for `size` (cheap for JPEGs)."""
    im = Image.open(path)
    im.draft(mode or "RGB", size)     # JPEG: decode at 1/2, 1/4 or 1/8 scale; no-op otherwise
    return im


def make_thumb(src, dst):
    """Upright, small JPEG thumbnail. Shrink first, then rotate the small image."""
    with _thumb_slots, open_small(src, THUMB_SIZE) as im:
        im.thumbnail(THUMB_SIZE)                  # keeps the EXIF, so the flag is still readable
        small = ImageOps.exif_transpose(im)       # rotate the 480px image, not the full photo
        small.convert("RGB").save(dst, "JPEG", quality=82)



FINGERPRINT_SCALES = (1.0, 0.9, 0.8, 0.7, 0.6)


def _dhash(gray):
    """256-bit difference hash: is each pixel brighter than its right neighbour?"""
    px = gray.resize((17, 16), Image.LANCZOS).tobytes()
    bits = 0
    for row in range(16):
        for col in range(16):
            i = row * 17 + col
            bits = (bits << 1) | (px[i] > px[i + 1])
    return bits


def fingerprints(src):
    """Perceptual fingerprints of a photo and of its centre at 90%..60% zoom,
    as space-separated hex. Built from a small grayscale copy, so they survive
    what phones and WhatsApp do to a photo (shrink, recompress, rotate), and the
    zoomed ones let a zoomed-in retake match the wider shot (see dedupe.py)."""
    with Image.open(src) as im:
        im.draft("RGB", (512, 512))
        im.thumbnail((512, 512))
        gray = ImageOps.exif_transpose(im).convert("L")
    w, h = gray.size
    out = []
    for s in FINGERPRINT_SCALES:
        cw, ch = int(w * s), int(h * s)
        left, top = (w - cw) // 2, (h - ch) // 2
        out.append(f"{_dhash(gray.crop((left, top, left + cw, top + ch))):064x}")
    return " ".join(out)
