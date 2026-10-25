"""Duplicates & Almost duplicates: one best shot per set of similar photos.

Guests send the same photo twice (or once on WhatsApp and again in full
quality), and take bursts and retakes of the same pose. For each set of
similar photos only the best-looking one stays in the album; the others move
to a folder, depending on how close they are to it:

  * Duplicates         - within DUP_DISTANCE: repeat copies, near-identical bursts
  * Almost duplicates  - within ALMOST_DISTANCE: close variations of the pose

Nothing is ever deleted — "moving" only sets photos.hidden = 1,
photos.dup_kind = 'duplicate' | 'almost' and photos.duplicate_of = the shot
the album shows for that set.

Distances are between zoom-tolerant fingerprints (imaging.fingerprints; a
zoomed-in retake is compared with the centre of the wider shot). Measured on
the real wedding photos: variations of one pose are 16-100 bits apart and
different groups of people at the same stage 84+. At 60 no different shots
were merged; 90 also catches the variations 60 missed (9/12, 14/15, 20/23)
but takes one look-alike pair of different shots (8/9) — the family can put
such a photo back with "Back to album".

Best-looking = highest resolution, then sharpest, then well exposed. It can't
judge closed eyes or smiles, so the family can override from the app:
"Use this one" (dup_pick), "Back to album" (dup_keep), "Move to Duplicates".
"""
import math
import os
import threading
import traceback

from PIL import Image

from . import imaging, store

DUP_DISTANCE = 60
ALMOST_DISTANCE = 90
DUPLICATES = "duplicates"          # pseudo-events the album uses for the two folders
ALMOST = "almost"
GROUPING_VERSION = 2               # 2 = photos are also grouped at the "almost" distance
_lock = threading.Lock()           # uploads and the startup pass both regroup


def parse(fp):
    return [int(x, 16) for x in fp.split()]


def distance(a, b):
    """Bits apart, allowing either photo to be a zoomed-in version of the other."""
    return min(min((a[0] ^ b[k]).bit_count(), (b[0] ^ a[k]).bit_count()) for k in range(len(a)))


def kind_for(d):
    return DUPLICATES if d <= DUP_DISTANCE else ALMOST


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


def _closest_visible(db, me, limit=ALMOST_DISTANCE):
    """(distance, row) of the most similar album photo within `limit`, or None.
    Only photos already sorted count, so the startup pass over older photos
    behaves exactly like uploads arriving one by one. A photo the family put
    "Back to album" only collects true copies of itself, not loose look-alikes."""
    mine, best = parse(me["dhash"]), None
    for r in db.execute("SELECT * FROM photos WHERE dhash IS NOT NULL AND COALESCE(hidden, 0) = 0"
                        " AND COALESCE(grouped, 0) >= ? AND id != ?", (GROUPING_VERSION, me["id"])):
        d = distance(mine, parse(r["dhash"]))
        if r["dup_keep"] and d > DUP_DISTANCE:
            continue
        if d <= limit and (best is None or d < best[0]):
            best = (d, r)
    return best


def _hide(db, pid, lead, kind):
    db.execute("UPDATE photos SET hidden = 1, duplicate_of = ?, dup_kind = ?, dup_pick = 0, dup_keep = 0"
               " WHERE id = ?", (lead, kind, pid))


def _repoint(db, old_lead, new_lead):
    """Photos filed under old_lead now belong to new_lead's set; re-measure which folder."""
    new = _row(db, new_lead)
    for r in db.execute("SELECT id, dhash, dup_kind FROM photos WHERE duplicate_of = ? AND id != ?",
                        (old_lead, new_lead)).fetchall():
        kind = r["dup_kind"] or DUPLICATES
        if r["dhash"] and new["dhash"]:
            kind = kind_for(distance(parse(r["dhash"]), parse(new["dhash"])))
        db.execute("UPDATE photos SET duplicate_of = ?, dup_kind = ? WHERE id = ?", (new_lead, kind, r["id"]))


def place(db, pid):
    """Decide whether a photo stays in the album or goes to a folder.
    Returns {"duplicate": bool, "kind": folder or None, "of": id or None,
    "moved": [ids]} — "moved" are photos this one beat, now in a folder."""
    with _lock:
        me = _row(db, pid)
        out = {"duplicate": False, "kind": None, "of": None, "moved": []}
        db.execute("UPDATE photos SET grouped = ? WHERE id = ?", (GROUPING_VERSION, pid))
        if me and me["dhash"] and not me["dup_keep"] and not me["hidden"]:
            hit = _closest_visible(db, me)
            if hit is not None:
                d, twin = hit
                fixed = twin["dup_pick"] or twin["dup_keep"]          # the family's choices are never undone
                if me["dup_pick"] and fixed:
                    pass                                              # two choices the family made: leave both
                elif me["dup_pick"] or (not fixed and quality(db, me) > quality(db, twin)):
                    out["moved"] = [twin["id"]] + [r["id"] for r in db.execute(
                        "SELECT id FROM photos WHERE duplicate_of = ?", (twin["id"],))]
                    _hide(db, twin["id"], pid, kind_for(d))
                    _repoint(db, twin["id"], pid)
                else:
                    _hide(db, pid, twin["id"], kind_for(d))
                    _repoint(db, pid, twin["id"])   # anything filed under this photo follows it
                    out.update(duplicate=True, kind=kind_for(d), of=twin["id"])
        db.commit()
        return out


def folder(db, kind):
    """Photos in a folder, set by set: the shot the album shows for the set
    first (in_album=True), then the photos filed under it."""
    members = db.execute("SELECT * FROM photos WHERE COALESCE(hidden, 0) = 1 AND COALESCE(dup_kind, ?) = ?"
                         " ORDER BY created_at", (DUPLICATES, kind)).fetchall()
    filed = {r["id"]: r["duplicate_of"] for r in db.execute("SELECT id, duplicate_of FROM photos WHERE hidden = 1")}
    def album_shot(lead):            # follow the chain to the photo the album actually shows
        seen = set()
        while lead in filed and lead not in seen:
            seen.add(lead)
            lead = filed[lead]
        return lead
    sets = {}
    for m in members:
        sets.setdefault(album_shot(m["duplicate_of"]), []).append(m)
    out, keys = [], [k for k in sets if k]
    leads = {r["id"]: r for r in db.execute(
        "SELECT * FROM photos WHERE id IN (%s)" % ",".join("?" * len(keys)), keys)} if keys else {}
    order = sorted(sets, key=lambda k: (leads[k]["created_at"] if k in leads else float("inf")))
    for n, lead in enumerate(order, 1):
        if lead in leads:
            out.append((leads[lead], n, True))
        out += [(m, n, False) for m in sets[lead]]
    return out


# ------------------------------------------------------------ family overrides
def use_this_one(db, pid):
    """Show this photo in the album instead of the shot currently chosen for its set."""
    with _lock:
        me = _row(db, pid)
        if not me:
            return False
        lead, kind = me["duplicate_of"], me["dup_kind"] or DUPLICATES
        db.execute("UPDATE photos SET hidden = 0, duplicate_of = NULL, dup_kind = NULL, dup_pick = 1, dup_keep = 0"
                   " WHERE id = ?", (pid,))
        if lead:
            _hide(db, lead, pid, kind)
            _repoint(db, lead, pid)
        db.commit()
        return True


def back_to_album(db, pid):
    """Not a duplicate after all: show it in the album and never group it again."""
    with _lock:
        db.execute("UPDATE photos SET hidden = 0, duplicate_of = NULL, dup_kind = NULL, dup_keep = 1 WHERE id = ?",
                   (pid,))
        db.commit()
        return True


def move_to_duplicates(db, pid):
    """A variation the automatic check missed: file it under the most similar album photo."""
    with _lock:
        me = _row(db, pid)
        if not me:
            return False
        hit = _closest_visible(db, me, limit=256) if me["dhash"] else None
        target = hit[1]["id"] if hit is not None else None
        _hide(db, pid, target, DUPLICATES)
        if target:
            _repoint(db, pid, target)
        db.commit()
        return True


# ------------------------------------------------------------- startup pass
def clean_up_existing():
    """Runs once in the background at startup (low memory, never blocks the
    site): fingerprint and analyse photos from before this feature, clear the
    old exact-match 'duplicate' tags (they wrongly hit burst shots), then sort
    every photo not yet grouped by the current rules, oldest first."""
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
        # photos filed before there were two folders were all Duplicates
        db.execute("UPDATE photos SET dup_kind = ? WHERE hidden = 1 AND dup_kind IS NULL", (DUPLICATES,))
        db.commit()
        moved = 0
        for r in db.execute("SELECT id FROM photos WHERE COALESCE(grouped, 0) < ? ORDER BY created_at",
                            (GROUPING_VERSION,)).fetchall():
            res = place(db, r["id"])
            moved += res["duplicate"] + len(res["moved"])
        count = lambda k: db.execute("SELECT COUNT(*) n FROM photos WHERE hidden = 1 AND dup_kind = ?",
                                     (k,)).fetchone()["n"]
        print(f"dedupe: fingerprinted {len(todo)} photo(s), cleared {len(tagged)} old duplicate tag(s), "
              f"{moved} move(s); now {count(DUPLICATES)} in Duplicates, {count(ALMOST)} in Almost duplicates",
              flush=True)
    except Exception:
        traceback.print_exc()
