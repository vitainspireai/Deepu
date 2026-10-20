"""Keep every photo upright.

Phones (iPhones especially) store pixels sideways and add an EXIF
"Orientation" flag that says how to rotate on display. Browsers obey it but
thumbnails, downloads and other apps often don't, so photos show up on their
side. `upright()` bakes the rotation into the pixels and clears the flag.
"""
import os

from PIL import Image, ImageOps

ORIENTATION = 0x0112


def upright(path):
    """Rotate the photo's pixels to match its EXIF orientation. Returns True
    if the file was rewritten. Photos that are already upright are untouched
    (no re-encoding, no quality loss)."""
    try:
        with Image.open(path) as im:
            if im.getexif().get(ORIENTATION, 1) in (None, 1):
                return False
            fmt = im.format or "JPEG"
            fixed = ImageOps.exif_transpose(im)          # also drops the orientation flag
            exif = fixed.getexif()
            tmp = path + ".tmp"
            if fmt == "JPEG":
                fixed.convert("RGB").save(tmp, "JPEG", quality=95, exif=exif)
            else:
                fixed.save(tmp, fmt)
        os.replace(tmp, path)
        return True
    except Exception as e:  # never lose an upload over a rotation problem
        print("upright failed:", path, e)
        try:
            os.remove(path + ".tmp")
        except OSError:
            pass
        return False


def fix_existing(media_dir, thumb_dir):
    """One-off sweep for photos uploaded before this existed: rotate them and
    drop their cached (sideways) thumbnails. Returns the file names it fixed.
    Cheap when nothing needs fixing."""
    fixed = []
    for name in os.listdir(media_dir):
        if name.endswith(".tmp"):
            continue
        if upright(os.path.join(media_dir, name)):
            fixed.append(name)
            try:
                os.remove(os.path.join(thumb_dir, name + ".jpg"))
            except OSError:
                pass
    return fixed
