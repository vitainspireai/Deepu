"""The fun leaderboard: AI picks the Top 3, re-ranked on every new upload.

The score is a transparent, playful formula — not a black box. Each pick
gets a fun "why" line generated from whatever actually earned it points,
so the leaderboard feels alive and gives guests a reason to keep shooting
for #1.
"""
import time

from .config import EVENT_LINES

TAG_BONUS = {"dancing": 8, "candid laughing": 7, "baraat": 6, "bride": 5,
             "groom": 5, "couple": 6, "decorations": 4, "selfie": 3,
             "garland exchange": 6, "sacred fire ceremony": 6, "dhol": 5,
             "mandap": 4, "mehndi hands": 4, "aarti": 4, "blessings": 5}


def _tags(db, photo_id):
    return {r["tag"]: r["score"] for r in
            db.execute("SELECT tag, score FROM photo_tags WHERE photo_id = ?", (photo_id,))}


def _face_count(db, photo_id):
    return db.execute("SELECT COUNT(*) n FROM photo_faces WHERE photo_id = ?",
                      (photo_id,)).fetchone()["n"]


def _applause(db, photo_id):
    return db.execute("SELECT COUNT(*) n FROM applause WHERE photo_id = ?",
                      (photo_id,)).fetchone()["n"]


def rescore(db, photo_id):
    """Recompute and store the score for one photo. Returns (score, reason)."""
    p = db.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not p:
        return 0, ""
    tags = _tags(db, photo_id)
    faces = _face_count(db, photo_id)
    claps = _applause(db, photo_id)
    bright = p["brightness"]

    parts, whys = [], []
    sharp = min(1.0, (p["blur_score"] or 0) / 300.0) * 25
    parts.append(sharp)
    if sharp >= 20:
        whys.append(("Tack sharp — not a blur in sight 🔍", sharp))
    if bright is not None and 60 <= bright <= 205:
        parts.append(15); whys.append(("Perfectly lit, no flash crimes 💡", 15))
    elif bright is not None:
        parts.append(6)
    fpts = min(20, faces * 5)
    parts.append(fpts)
    if faces >= 6:
        whys.append((f"Poora parivaar! {faces} happy faces in one frame 🥳", fpts))
    elif faces >= 2:
        whys.append((f"{faces} smiling faces — double the joy 😄", fpts))
    elif faces == 1:
        whys.append(("A star turn, front and center ⭐", fpts))
    tb = 0
    for tag, bonus in TAG_BONUS.items():
        if tag in tags:
            tb += bonus
            whys.append((f"AI spotted: {tag} 🎯", bonus))
    parts.append(min(15, tb))
    if "duplicate" not in tags:
        parts.append(5)
    else:
        parts.append(-20); whys.append(("Seen this one before — encore! 🔁", -20))
    if "blurry" not in tags:
        parts.append(5)
    apts = min(24, claps * 3)
    parts.append(apts)
    if claps >= 3:
        whys.append((f"Crowd favorite — {claps} applauds 👏", apts))
    age_h = (time.time() - p["created_at"]) / 3600
    fresh = max(0.0, 6 - age_h)
    parts.append(fresh)
    if age_h < 2:
        whys.append(("Fresh off the camera, still warm 📸", fresh))
    ev = p["event"] or "other"
    parts.append(2)
    whys.append((EVENT_LINES.get(ev, EVENT_LINES["other"]), 2))

    score = round(max(0, sum(parts)), 1)
    whys.sort(key=lambda w: -w[1])
    reason = " · ".join(w[0] for w in whys[:2])
    db.execute("UPDATE photos SET score = ? WHERE id = ?", (score, photo_id))
    db.commit()
    return score, reason


def leader_id(db, exclude=None):
    order = "ORDER BY score DESC, created_at DESC LIMIT 1"
    if exclude:
        r = db.execute(f"SELECT id FROM photos WHERE id != ? {order}",
                       (exclude,)).fetchone()
    else:
        r = db.execute(f"SELECT id FROM photos {order}").fetchone()
    return r["id"] if r else None


# ------------------------------------------------------------- category crowns
OUTFIT_TAGS = {"traditional attire", "elegant outfit", "colorful saree", "silk saree",
               "lehenga", "sherwani", "kurta", "turban", "bangles",
               "stylish pose", "jewelry"}
COUPLE_TAGS = {"couple", "bride", "groom", "romantic couple", "garland exchange"}
CANDID_TAGS = {"candid laughing", "dancing", "singing", "children playing", "dhol"}


def _cat_score(db, photo_id, wanted, solo_bonus_tags=None):
    """Category score: base score + tag evidence + family votes (counted
    double here — in a category, the crowd's taste is the point)."""
    p = db.execute("SELECT score FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not p:
        return 0
    tags = _tags(db, photo_id)
    faces = _face_count(db, photo_id)
    hits = sum(tags.get(t, 0) for t in wanted)
    s = p["score"] * 0.5 + hits * 30 + _applause(db, photo_id) * 2
    if solo_bonus_tags and faces and 1 <= faces <= 3:
        s += 6  # portraits and small groups show outfits best
    return round(s, 1)


def podium(db, category, n=3):
    if category == "overall":
        rows = db.execute(
            "SELECT id FROM photos WHERE score > 0 ORDER BY score DESC, created_at DESC LIMIT ?",
            (n,)).fetchall()
        ids = [r["id"] for r in rows]
    else:
        wanted = {"best_dressed": OUTFIT_TAGS, "couple": COUPLE_TAGS,
                  "candid": CANDID_TAGS}[category]
        cand = []
        for r in db.execute("SELECT id FROM photos").fetchall():
            cs = _cat_score(db, r["id"], wanted, solo_bonus_tags=True)
            tags = _tags(db, r["id"])
            if cs > 10 and (wanted & set(tags) or category == "candid"):
                cand.append((cs, r["id"]))
        cand.sort(reverse=True)
        ids = [pid for _, pid in cand[:n]]
    out, medals = [], ["🥇", "🥈", "🥉"]
    for i, pid in enumerate(ids):
        row = dict(db.execute("SELECT * FROM photos WHERE id = ?", (pid,)).fetchone())
        _, row["reason"] = rescore(db, pid)
        row["medal"] = medals[i]
        row["applause"] = _applause(db, pid)
        out.append(row)
    return out


def cover_of_the_day(db, day_start_epoch):
    r = db.execute(
        "SELECT * FROM photos WHERE created_at >= ? ORDER BY score DESC, created_at DESC LIMIT 1",
        (day_start_epoch,)).fetchone()
    if not r:
        r = db.execute("SELECT * FROM photos ORDER BY score DESC, created_at DESC LIMIT 1").fetchone()
    return dict(r) if r else None


# --------------------------------------------------------------------- badges
def award_badges(db, uploader, photo_id, now, day_start, hour):
    """Returns names of badges NEWLY earned by this uploader on this upload."""
    have = {r["badge"] for r in db.execute(
        "SELECT badge FROM badges WHERE uploader = ?", (uploader,))}
    earned = []

    def give(badge):
        if badge not in have:
            db.execute("INSERT OR IGNORE INTO badges VALUES (?,?,?)",
                       (uploader, badge, now))
            earned.append(badge)
    total = db.execute("SELECT COUNT(*) n FROM photos WHERE uploader = ?",
                       (uploader,)).fetchone()["n"]
    today_first = db.execute(
        "SELECT COUNT(*) n FROM photos WHERE created_at >= ? AND id != ?",
        (day_start, photo_id)).fetchone()["n"] == 0
    if total == 1:
        give("Shaadi Debut 📸")
    if total >= 25:
        give("Photographer Uncle 🎯")
    if today_first:
        give("Subah Ka Star 🌅")
    if hour >= 23 or hour < 4:
        give("Midnight Baraati 🦉")
    claps = db.execute(
        "SELECT COUNT(*) n FROM applause a JOIN photos p ON p.id = a.photo_id"
        " WHERE p.uploader = ?", (uploader,)).fetchone()["n"]
    if claps >= 20:
        give("Family Favourite ⭐")
    if leader_id(db) == photo_id:
        give("Crown Holder 👑")
    db.commit()
    return earned
