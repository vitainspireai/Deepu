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
