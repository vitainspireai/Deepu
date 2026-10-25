"""Duplicates folder: one best shot per set of near-identical photos.

Guests send the same photo twice (or once on WhatsApp and again in full
quality), and take bursts and retakes of the same pose. For each set of
such photos, the best-looking one stays in the album and the others move to
the 🔁 Duplicates folder. Nothing is ever deleted — "moving" only sets
photos.hidden = 1 and photos.duplicate_of = the shot the album shows.

Same set = fingerprints within DUP_DISTANCE bits, allowing for zoom (a
zoomed-in retake is compared with the centre of the wider shot). Measured on
the real wedding photos: variations of one pose 16-100 bits apart, different
groups of people at the same stage 84+; at 60 no different shots were merged.

Almost duplicates: album photos within ALMOST_DISTANCE of each other but not
close enough to move automatically. They stay in the album and are listed, in
sets of look-alikes, for the family to review ("Move to Duplicates" or "Not a
duplicate"). On the real photos, 90 caught all three missed variations
(9/12, 14/15, 20/23) plus one look-alike pair of different shots (8/9).

Best-looking = highest resolution, then sharpest, then well exposed. It can't
judge closed eyes or smiles, so the family can override from the app:
"Use this one" (dup_pick), "Back to album" (dup_keep), "Move to Duplicates"
and "Not a duplicate" (dismisses its almost-duplicate pairs).
"""
import math
import os
import threading
import traceback

from PIL import Image

from . import imaging, store

DUP_DISTANCE = 60
ALMOST_DISTANCE = 90
DUPLICATES = "duplicates"          # pseudo-events the album uses for the two views
ALMOST = "almost"
_lock = threading.Lock()           # uploads and the startup pass both regroup


def parse(fp):
    return [int(x, 16) for x in fp.split()]


def distance(a, b):
    """Bits apart, allowing either photo to be a zoomed-in version of the other."""
    return min(min((a[0] ^ b[k]).bit_count(), (b[0] ^ a[k]).bit_count()) for k in range(len(a)))


def _pixels(row):
    w, h = row["width"], row["height"]
    if not (w and h):
        try:
            with Image.open(os.path.join(store.MEDIA_DIR, row["filename"])) as im:
                w, h = im.size
        except Exception:
            return 1
    return max(1, w * h)


def quality(db, row):
    """Higher = better-looking copy. Resolution dominates (so a full-quality
    upload beats a WhatsApp copy of the same photo), then sharpness, then exposure."""
    q = 10 * math.log2(_pixels(row)) + (row["blur_score"] or 0) / 100
    b = row["brightness"]
    if b is not None and not 60 <= b <= 205:
        q -= 20
    q += 2 * db.execute("SELECT COUNT(*) n FROM photo_faces WHERE photo_id = ?", (row["id"],)).fetchone()["n"]
    return q


def _row(db, pid):
    return db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone()


def _closest_visible(db, me, limit=DUP_DISTANCE):
    mine, best = parse(me["dhash"]), None
    for r in db.execute("SELECT * FROM photos WHERE dhash IS NOT NULL AND COALESCE(hidden, 0) = 0"
                        " AND id != ?", (me["id"],)):
        d = distance(mine, parse(r["dhash"]))
        if d <= limit and (best is None or d < best[0]):
            best = (d, r)
    return best[1] if best else None


def _record_pairs(db, me):
    """Remember every photo that looks like this one (within ALMOST_DISTANCE),
    in or out of the album, so the review list never has to compare all pairs."""
    if me["dhash"]:
        mine = parse(me["dhash"])
        for r in db.execute("SELECT id, dhash FROM photos WHERE dhash IS NOT NULL AND id != ?", (me["id"],)).fetchall():
            d = distance(mine, parse(r["dhash"]))
            if d <= ALMOST_DISTANCE:
                a, b = sorted((me["id"], r["id"]))
                db.execute("INSERT OR IGNORE INTO near_pairs (a, b, d) VALUES (?,?,?)", (a, b, d))
    db.execute("UPDATE photos SET paired = 1 WHERE id = ?", (me["id"],))


def almost_sets(db):
    """{photo id: set number} for album photos with an undismissed look-alike in
    the album. Look-alikes are joined into sets; sets are numbered oldest first."""
    parent = {}
    def root(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for r in db.execute("SELECT np.a, np.b FROM near_pairs np"
                        " JOIN photos pa ON pa.id = np.a JOIN photos pb ON pb.id = np.b"
                        " WHERE np.dismissed = 0 AND COALESCE(pa.hidden, 0) = 0 AND COALESCE(pb.hidden, 0) = 0"):
        parent[root(r["a"])] = root(r["b"])
    if not parent:
        return {}
    marks = ",".join("?" * len(parent))
    order = [r["id"] for r in db.execute(f"SELECT id FROM photos WHERE id IN ({marks}) ORDER BY created_at", list(parent))]
    numbers, out = {}, {}
    for pid in order:
        out[pid] = numbers.setdefault(root(pid), len(numbers) + 1)
    return out


def place(db, pid):
    """Decide whether a newly analysed photo stays in the album or goes to
    Duplicates. Returns {"duplicate": bool, "of": id or None, "moved": [ids]}
    — "moved" are photos this one beat, now in Duplicates."""
    with _lock:
        me = _row(db, pid)
        out = {"duplicate": False, "of": None, "moved": []}
        db.execute("UPDATE photos SET grouped = 1 WHERE id = ?", (pid,))
        if me and me["dhash"] and not me["dup_keep"] and not me["hidden"]:
            twin = _closest_visible(db, me)
            if twin is not None:
                if not twin["dup_pick"] and not twin["dup_keep"] and quality(db, me) > quality(db, twin):
                    out["moved"] = [twin["id"]] + [r["id"] for r in db.execute(
                        "SELECT id FROM photos WHERE duplicate_of = ?", (twin["id"],))]
                    db.execute("UPDATE photos SET hidden = 1, duplicate_of = ? WHERE id = ? OR duplicate_of = ?",
                               (pid, twin["id"], twin["id"]))
                else:
                    db.execute("UPDATE photos SET hidden = 1, duplicate_of = ? WHERE id = ?", (twin["id"], pid))
                    out.update(duplicate=True, of=twin["id"])
        if me:
            _record_pairs(db, me)
        db.commit()
        return out


# ------------------------------------------------------------ family overrides
def use_this_one(db, pid):
    """Show this photo in the album instead of the shot currently chosen for its set."""
    with _lock:
        me = _row(db, pid)
        if not me:
            return False
        lead = me["duplicate_of"]
        db.execute("UPDATE photos SET hidden = 0, duplicate_of = NULL, dup_pick = 1, dup_keep = 0 WHERE id = ?", (pid,))
        if lead:
            db.execute("UPDATE photos SET hidden = 1, duplicate_of = ?, dup_pick = 0, dup_keep = 0 WHERE id = ?",
                       (pid, lead))
            db.execute("UPDATE photos SET duplicate_of = ? WHERE duplicate_of = ? AND id != ?", (pid, lead, pid))
        db.commit()
        return True


def back_to_album(db, pid):
    """Not a duplicate after all: show it in the album and never group it again."""
    with _lock:
        db.execute("UPDATE photos SET hidden = 0, duplicate_of = NULL, dup_keep = 1 WHERE id = ?", (pid,))
        db.execute("UPDATE near_pairs SET dismissed = 1 WHERE a = ? OR b = ?", (pid, pid))   # already judged
        db.commit()
        return True


def not_a_duplicate(db, pid):
    """Almost duplicates review: this photo is its own moment — stop flagging
    it against the look-alikes it has now (new look-alikes are still flagged)."""
    with _lock:
        db.execute("UPDATE near_pairs SET dismissed = 1 WHERE a = ? OR b = ?", (pid, pid))
        db.commit()
        return True


def move_to_duplicates(db, pid):
    """A variation the automatic check missed: file it under the most similar album photo."""
    with _lock:
        me = _row(db, pid)
        if not me:
            return False
        twin = _closest_visible(db, me, limit=256) if me["dhash"] else None
        target = twin["id"] if twin is not None else None
        db.execute("UPDATE photos SET hidden = 1, duplicate_of = ?, dup_pick = 0, dup_keep = 0 WHERE id = ?",
                   (target, pid))
        if target:
            db.execute("UPDATE photos SET duplicate_of = ? WHERE duplicate_of = ?", (target, pid))
        db.commit()
        return True


# ------------------------------------------------------------- startup pass
def clean_up_existing():
    """Runs once in the background at startup (low memory, never blocks the
    site): fingerprint and analyse photos from before this feature, clear the
    old exact-match 'duplicate' tags (they wrongly hit burst shots), then sort
    every not-yet-grouped photo, oldest first, exactly as if it had just arrived."""
    try:
        from . import rank
        from .ai_worker import analyze_one
        db = store.connect()
        todo = db.execute("SELECT id, filename FROM photos WHERE dhash IS NULL").fetchall()
        for r in todo:
            try:
                db.execute("UPDATE photos SET dhash = ? WHERE id = ?",
                           (imaging.fingerprints(os.path.join(store.MEDIA_DIR, r["filename"])), r["id"]))
                db.commit()
            except Exception as e:
                print("fingerprint failed:", r["filename"], e, flush=True)
        for r in db.execute("SELECT id FROM photos WHERE blur_score IS NULL").fetchall():
            analyze_one(db, r["id"])       # e.g. Memory Lane photos were never analysed
        tagged = [r["photo_id"] for r in db.execute("SELECT photo_id FROM photo_tags WHERE tag = 'duplicate'")]
        if tagged:
            db.execute("DELETE FROM photo_tags WHERE tag = 'duplicate'")
            db.commit()
            for pid in tagged:
                rank.rescore(db, pid)
        moved = 0
        for r in db.execute("SELECT id FROM photos WHERE COALESCE(grouped, 0) = 0 ORDER BY created_at").fetchall():
            res = place(db, r["id"])
            moved += res["duplicate"] + len(res["moved"])
        for r in db.execute("SELECT * FROM photos WHERE COALESCE(paired, 0) = 0 ORDER BY created_at").fetchall():
            with _lock:                     # photos sorted before the almost-duplicates review existed
                _record_pairs(db, r)
                db.commit()
        hidden = db.execute("SELECT COUNT(*) n FROM photos WHERE hidden = 1").fetchone()["n"]
        print(f"dedupe: fingerprinted {len(todo)} photo(s), cleared {len(tagged)} old duplicate tag(s), "
              f"{moved} move(s) to Duplicates, {hidden} photo(s) now in Duplicates, "
              f"{len(almost_sets(db))} almost-duplicate(s) to review", flush=True)
    except Exception:
        traceback.print_exc()
