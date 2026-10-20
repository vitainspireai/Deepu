"""AI worker — run after uploads:  python -m app.ai_worker

Always-on (Pillow only):
  * perceptual hash + duplicate marking (tag 'duplicate')
  * blur score (very blurry shots tagged 'blurry', hidden from highlights)

Optional (pip install -r requirements-ai.txt):
  * Face grouping  — every distinct face becomes a "person"; the gallery
    shows them as circles at the top. Family taps a face and types the name
    once ("Deepu", "Krishna", "Ravi Uncle") and can then see/share ALL their photos.
    Uses insightface if available, else face_recognition.
  * Smart tags — CLIP zero-shot tags (bride, groom, dancing, food, decor…)
    power the gallery search box.
"""
import os
import sys

from PIL import Image, ImageFilter, ImageStat

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import store  # noqa: E402

WEDDING_TAGS = ["bride", "groom", "couple", "romantic couple", "dancing",
                "singing", "food", "cake cutting", "decorations", "flowers",
                "family group", "children playing", "baraat", "dhol",
                "haldi ceremony", "mehndi hands", "sangeet", "blessings",
                "feet touching blessings", "aarti", "garland exchange",
                "sacred fire ceremony", "mandap", "marigold flowers",
                "rangoli", "vidaai", "candid laughing", "selfie",
                "traditional attire", "elegant outfit", "colorful saree",
                "silk saree", "lehenga", "sherwani", "kurta", "turban",
                "bangles", "stylish pose", "jewelry"]


def ahash(path):
    with Image.open(path) as im:
        g = im.convert("L").resize((8, 8))
        px = list(g.getdata())
    avg = sum(px) / 64
    return "".join("1" if p > avg else "0" for p in px)


def blur_score(path):
    with Image.open(path) as im:
        g = im.convert("L")
        g.thumbnail((512, 512))
        edges = g.filter(ImageFilter.FIND_EDGES)
        return ImageStat.Stat(edges).var[0]


def brightness(path):
    with Image.open(path) as im:
        g = im.convert("L")
        g.thumbnail((256, 256))
        return ImageStat.Stat(g).mean[0]


def analyze_one(db, photo_id):
    """Instant per-photo analysis at upload time (hash, blur, brightness)
    so the Top 3 leaderboard reacts immediately; faces/tags stay in the
    batch worker. Ends by re-scoring the photo."""
    from app import rank
    r = db.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not r:
        return
    path = os.path.join(store.MEDIA_DIR, r["filename"])
    try:
        h, b, br = ahash(path), blur_score(path), brightness(path)
    except Exception:
        return
    db.execute("UPDATE photos SET phash = ?, blur_score = ?, brightness = ? WHERE id = ?",
               (h, b, br, photo_id))
    dup = db.execute("SELECT id FROM photos WHERE phash = ? AND id != ? LIMIT 1",
                     (h, photo_id)).fetchone()
    if dup:
        db.execute("INSERT OR IGNORE INTO photo_tags VALUES (?,?,?)", (photo_id, "duplicate", 1.0))
    if b < 40:
        db.execute("INSERT OR IGNORE INTO photo_tags VALUES (?,?,?)", (photo_id, "blurry", 1.0))
    db.commit()
    rank.rescore(db, photo_id)


def quality_pass(db):
    seen = {}
    rows = db.execute("SELECT id, filename FROM photos WHERE ai_done = 0").fetchall()
    for r in rows:
        path = os.path.join(store.MEDIA_DIR, r["filename"])
        try:
            h = ahash(path)
            b = blur_score(path)
        except Exception:
            continue
        db.execute("UPDATE photos SET phash = ?, blur_score = ? WHERE id = ?", (h, b, r["id"]))
        if h in seen:
            db.execute("INSERT OR IGNORE INTO photo_tags VALUES (?,?,?)", (r["id"], "duplicate", 1.0))
        else:
            seen[h] = r["id"]
        if b < 40:
            db.execute("INSERT OR IGNORE INTO photo_tags VALUES (?,?,?)", (r["id"], "blurry", 1.0))
    db.commit()
    return [r["id"] for r in rows]


# ---------------------------------------------------------------- faces (opt)
def rebuild_people(db, clusters):
    """Replace the people/face tables with freshly found clusters (sets of
    photo ids) WITHOUT losing what the family already typed: each new cluster
    inherits the id and name of the old person whose photos it overlaps most,
    so names and shared ?person= links survive re-runs."""
    old = {}  # old person id -> [name, cover, set(photo ids)]
    for r in db.execute("SELECT id, name, cover_photo FROM people"):
        old[r["id"]] = [r["name"], r["cover_photo"], set()]
    for r in db.execute("SELECT photo_id, person_id FROM photo_faces"):
        if r["person_id"] in old:
            old[r["person_id"]][2].add(r["photo_id"])
    kept = [c for c in clusters if len(c) >= 2]          # a face seen once isn't a "person"
    pairs = sorted(((len(c & o[2]), i, oid) for i, c in enumerate(kept)
                    for oid, o in old.items() if c & o[2]), reverse=True)
    assign, used = {}, set()
    for overlap, i, oid in pairs:                          # best overlaps first, one-to-one
        if i in assign or oid in used:
            continue
        if overlap >= 0.5 * min(len(kept[i]), len(old[oid][2])):
            assign[i] = oid
            used.add(oid)
    db.execute("DELETE FROM photo_faces")
    db.execute("DELETE FROM people")
    for i, photos in enumerate(kept):
        oid = assign.get(i)
        name = old[oid][0] if oid else None
        cover = old[oid][1] if oid and old[oid][1] in photos else next(iter(photos))
        person = oid or store.new_id()
        db.execute("INSERT INTO people (id, name, cover_photo, face_count) VALUES (?,?,?,?)",
                   (person, name, cover, len(photos)))
        for pid in photos:
            db.execute("INSERT OR IGNORE INTO photo_faces VALUES (?,?)", (pid, person))
    db.commit()


def face_pass(db):
    try:
        import insightface  # noqa
        engine = "insightface"
    except ImportError:
        try:
            import face_recognition  # noqa
            engine = "face_recognition"
        except ImportError:
            print("face grouping skipped (install requirements-ai.txt)")
            return
    embeddings = []  # (photo_id, vector)
    if engine == "insightface":
        import insightface
        model = insightface.app.FaceAnalysis(name="buffalo_l")
        model.prepare(ctx_id=-1)
        for r in db.execute("SELECT id, filename FROM photos").fetchall():
            faces = model.get(os.path.join(store.MEDIA_DIR, r["filename"]))
            for f in faces:
                embeddings.append((r["id"], list(f.normed_embedding)))
        def dist(a, b):
            return 1 - sum(x * y for x, y in zip(a, b))
        thresh = 0.45
    else:
        import face_recognition
        for r in db.execute("SELECT id, filename FROM photos").fetchall():
            img = face_recognition.load_image_file(os.path.join(store.MEDIA_DIR, r["filename"]))
            for enc in face_recognition.face_encodings(img):
                embeddings.append((r["id"], list(enc)))
        def dist(a, b):
            import math
            return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
        thresh = 0.5
    clusters = []  # (rep_vector, set(photo_ids))
    for pid, vec in embeddings:
        for rep, photos in clusters:
            if dist(vec, rep) < thresh:
                photos.add(pid)
                break
        else:
            clusters.append((vec, {pid}))
    if not clusters:
        print("no faces found — leaving existing people untouched")
        return
    rebuild_people(db, [c[1] for c in clusters])
    print(f"face grouping done: {len(clusters)} faces clusters")


# ----------------------------------------------------------------- tags (opt)
def tag_pass(db):
    try:
        import open_clip
        import torch
    except ImportError:
        print("smart tags skipped (install requirements-ai.txt)")
        return
    model, _, preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="openai")
    tokenizer = open_clip.get_tokenizer("ViT-B-32")
    texts = tokenizer([f"a wedding photo of {t}" for t in WEDDING_TAGS])
    with torch.no_grad():
        tf = model.encode_text(texts)
        tf /= tf.norm(dim=-1, keepdim=True)
    for r in db.execute("SELECT id, filename FROM photos").fetchall():
        try:
            img = preprocess(Image.open(os.path.join(store.MEDIA_DIR, r["filename"])).convert("RGB")).unsqueeze(0)
        except Exception:
            continue
        with torch.no_grad():
            f = model.encode_image(img)
            f /= f.norm(dim=-1, keepdim=True)
            scores = (f @ tf.T).softmax(dim=-1)[0]
        for tag, s in zip(WEDDING_TAGS, scores.tolist()):
            if s > 0.18:
                db.execute("INSERT OR REPLACE INTO photo_tags VALUES (?,?,?)", (r["id"], tag, s))
    db.commit()
    print("smart tags done")


def main():
    db = store.connect()
    done = quality_pass(db)
    face_pass(db)
    tag_pass(db)
    db.execute("UPDATE photos SET ai_done = 1 WHERE ai_done = 0")
    db.commit()
    print(f"AI pass complete ({len(done)} new photos processed)")


if __name__ == "__main__":
    main()
