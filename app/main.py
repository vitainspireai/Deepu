"""Wedding photo share — WhatsApp in, web gallery out.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
Guests either (a) send photos straight to the WhatsApp number, or
(b) tap the upload link shared in the WhatsApp group (full-resolution,
no WhatsApp compression). Anyone with the gallery link can view —
no WhatsApp group membership needed.
"""
import io
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
                               StreamingResponse)
from PIL import Image

from . import config, rank, store, wa
from .ai_worker import analyze_one

app = FastAPI(title="Wedding Photo Share")


@app.middleware("http")
async def no_cache_pages(request: Request, call_next):
    """HTML pages embed the live function list and settings — never serve a stale copy."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp
EVENTS = config.EVENTS
TZ = ZoneInfo(os.environ.get("EVENT_TZ", "America/Chicago"))


def check_token(t: str):
    # Render's generated tokens are base64 and may contain '+', which a
    # query string turns into a space — undo that before comparing.
    return bool(t) and t.replace(" ", "+") == wa.GALLERY_TOKEN


def _day():
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.timestamp(), start.timestamp(), now.hour


def announce_upload(db, photo_id, uploader):
    """Score a fresh photo instantly and work out the fun bits: its rank,
    whether it stole the crown, and any badges the uploader just earned."""
    before = rank.leader_id(db, exclude=photo_id)
    analyze_one(db, photo_id)
    score, reason = rank.rescore(db, photo_id)
    after = rank.leader_id(db)
    pos = db.execute("SELECT COUNT(*) n FROM photos WHERE score > ?",
                     (score,)).fetchone()["n"] + 1
    now_ts, day_start, hour = _day()
    badges = rank.award_badges(db, uploader, photo_id, now_ts, day_start, hour)
    return {"photo_id": photo_id, "score": score, "reason": reason,
            "position": pos, "crown": after == photo_id and before != photo_id,
            "dethroned_id": before if (after == photo_id and before
                                        and before != photo_id) else None,
            "badges": badges}


# ---------------------------------------------------------------- WhatsApp in
@app.get("/webhook")
async def webhook_verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == os.environ.get("WA_VERIFY_TOKEN"):
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


@app.post("/webhook")
async def webhook_receive(request: Request):
    payload = await request.json()
    db = store.connect()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                sender = msg.get("from", "")
                name = wa.profile_name(value, sender)
                mtype = msg.get("type")
                if mtype in ("image", "document"):
                    media = msg.get(mtype, {})
                    mime = media.get("mime_type", "")
                    if mtype == "image" or mime.startswith("image/"):
                        ext = ".jpg" if "png" not in mime else ".png"
                        fname = f"wa_{msg['id'][-12:]}{ext}"
                        dest = os.path.join(store.MEDIA_DIR, fname)
                        try:
                            wa.download_media(media["id"], dest)
                            w, h = _dims(dest)
                            pid = store.add_photo(db, filename=fname, orig_name=media.get("filename"),
                                                  source="whatsapp", uploader=name, uploader_wa=sender,
                                                  caption=msg.get("caption") or media.get("caption"),
                                                  width=w, height=h)
                            res = announce_upload(db, pid, name)
                            line1 = (f"👑 STOP THE PRESSES! Your photo just took the #1 crown! {res['reason']}"
                                     if res["crown"]
                                     else f"Got it, {name.split()[0]}! 📸 Your photo is ranked #{res['position']}. {res['reason']}")
                            badge_line = "".join(f"\n🏅 New badge: {b}" for b in res["badges"])
                            wa.send_text(sender,
                                         f"{line1}{badge_line}\n"
                                         f"See the live leaderboard & add full-quality photos: {wa.gallery_link()}")
                            if res["dethroned_id"]:
                                old = db.execute("SELECT uploader, uploader_wa FROM photos WHERE id = ?",
                                                 (res["dethroned_id"],)).fetchone()
                                if old and old["uploader_wa"] and old["uploader_wa"] != sender:
                                    try:
                                        wa.send_text(old["uploader_wa"],
                                                     f"👑 Drama! {name.split()[0]}'s new photo just knocked you off the #1 crown. "
                                                     "Send another one and take it back!")
                                    except Exception:
                                        pass
                        except Exception as e:  # keep webhook 200 so Meta doesn't retry-storm
                            print("media save failed:", e)
                    elif mtype == "document":
                        try:
                            wa.send_text(sender, "That file type can't go in the album — please send photos as images. 🙏")
                        except Exception as e:
                            print("reply failed:", e)
                elif mtype == "text":
                    body = (msg.get("text") or {}).get("body", "").strip().lower()
                    if body in ("link", "album", "photos", "gallery", "hi", "hello", "namaste"):
                        try:
                            wa.send_text(sender,
                                         f"Namaste! 🙏 Send me your photos from {config.COUPLE}'s wedding and I'll add them to the family album. 🪔\n"
                                         f"📸 View all photos, and tap “Add photos” to upload full-quality ones: {wa.gallery_link()}")
                        except Exception as e:
                            print("reply failed:", e)
    return {"status": "ok"}


# ------------------------------------------------------------------ web upload
@app.post("/api/upload")
async def api_upload(t: str = Form(""), uploader: str = Form("Family"),
                     event: str = Form("other"), year: str = Form(""), story: str = Form(""),
                     files: list[UploadFile] = File(...)):
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    if event not in EVENTS:
        event = "other"
    db = store.connect()
    saved, results = [], []
    for f in files:
        raw = await f.read()
        try:
            img = Image.open(io.BytesIO(raw)); img.verify()
        except Exception:
            continue
        ext = os.path.splitext(f.filename or "photo.jpg")[1].lower() or ".jpg"
        fname = f"web_{int(time.time()*1000)}_{len(saved)}{ext}"
        dest = os.path.join(store.MEDIA_DIR, fname)
        with open(dest, "wb") as out:
            out.write(raw)
        w, h = _dims(dest)
        memory = event == config.MEMORY
        pid = store.add_photo(db, filename=fname, orig_name=f.filename,
                              source="web", uploader=uploader or "Family",
                              event=event, width=w, height=h,
                              caption=(story.strip()[:200] or None) if memory else None,
                              memory_year=(re.sub(r"\D", "", year)[:4] or None) if memory else None)
        saved.append(pid)
        if memory:  # throwbacks aren't scored or ranked — they just join Memory Lane
            results.append({"photo_id": pid, "memory": True,
                            "badges": rank.award_memory_badge(db, uploader or "Family", time.time())})
        else:
            results.append(announce_upload(db, pid, uploader or "Family"))
    return {"saved": saved, "count": len(saved), "results": results}


def _dims(path):
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None, None


# ------------------------------------------------------------------ gallery API
def _select_photos(db, person="", event="", q="", uploader="", limit=300):
    sql = ("SELECT DISTINCT p.* FROM photos p "
           "LEFT JOIN photo_faces pf ON pf.photo_id = p.id "
           "LEFT JOIN photo_tags pt ON pt.photo_id = p.id WHERE 1=1")
    args = []
    if person:
        sql += " AND pf.person_id = ?"; args.append(person)
    if event:
        sql += " AND p.event = ?"; args.append(event)
    if uploader:
        sql += " AND p.uploader = ?"; args.append(uploader)
    if q:
        like = [f"%{q}%"] + [f"%{a}%" for a in config.SEARCH_ALIASES.get(q.strip().lower(), [])]
        sql += (" AND (" + " OR ".join(["pt.tag LIKE ?"] * len(like)) +
                " OR p.caption LIKE ? OR p.uploader LIKE ?)")
        args += like + [like[0]] * 2
    if not event:  # the live album never mixes in old memories
        sql += f" AND COALESCE(p.event, '') != '{config.MEMORY}'"
    sql += " ORDER BY p.created_at DESC LIMIT ?"; args.append(limit)
    return db.execute(sql, args).fetchall()


@app.get("/api/photos")
def api_photos(t: str = "", person: str = "", event: str = "", q: str = "",
               uploader: str = "", limit: int = 300):
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    rows = []
    for r in _select_photos(db, person, event, q, uploader, limit):
        row = dict(r)
        row["applause"] = db.execute("SELECT COUNT(*) n FROM applause WHERE photo_id = ?",
                                     (r["id"],)).fetchone()["n"]
        rows.append(row)
    people = [dict(r) for r in db.execute(
        "SELECT * FROM people WHERE face_count > 0 ORDER BY face_count DESC")]
    counts = {r["event"]: r["n"] for r in db.execute(
        "SELECT event, COUNT(*) n FROM photos GROUP BY event")}
    smart = db.execute("SELECT COUNT(*) n FROM photo_tags WHERE tag NOT IN ('duplicate', 'blurry')").fetchone()["n"]
    return {"photos": rows, "people": people, "event_counts": counts, "smart_tags": smart > 0}


@app.get("/api/pulse")
def api_pulse(t: str = "", since: float = 0):
    """Cheap heartbeat the album and TV pages poll every few seconds: a
    fingerprint of what changed, plus any photos newer than `since`."""
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    row = db.execute("SELECT COUNT(*) n, COALESCE(MAX(created_at), 0) latest FROM photos").fetchone()
    new = [dict(r) for r in db.execute(
        "SELECT id, uploader, caption, created_at, event, memory_year FROM photos WHERE created_at > ?"
        " ORDER BY created_at LIMIT 20", (since,))]
    return {"count": row["n"], "latest": row["latest"],
            "applause": db.execute("SELECT COUNT(*) n FROM applause").fetchone()["n"],
            "leader": rank.leader_id(db), "new": new, "now": time.time()}


@app.get("/api/download")
def api_download(t: str = "", person: str = "", event: str = "", q: str = "", uploader: str = ""):
    """Zip of the current filter (an event, a person, one uploader's photos, or all)."""
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    rows = _select_photos(db, person, event, q, uploader, limit=100000)
    tmp = tempfile.SpooledTemporaryFile(max_size=64 << 20)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:  # JPEGs are already compressed
        for i, r in enumerate(rows, 1):
            path = os.path.join(store.MEDIA_DIR, r["filename"])
            if os.path.exists(path):
                who = re.sub(r"[^\w-]+", "_", r["uploader"] or "family")[:30]
                z.write(path, f"{r['event'] or 'other'}/{i:04d}_{who}{os.path.splitext(r['filename'])[1]}")
    tmp.seek(0)

    def stream():
        try:
            while chunk := tmp.read(1 << 20):
                yield chunk
        finally:
            tmp.close()
    return StreamingResponse(stream(), media_type="application/zip",
                             headers={"Content-Disposition": 'attachment; filename="wedding-photos.zip"'})


@app.get("/api/top3")
def api_top3(t: str = ""):
    """Leaderboard: category podiums, cover of the day, honor roll,
    and unnamed faces for the Guess Who game."""
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    _, day_start, _ = _day()
    return {
        "podiums": {
            "overall": rank.podium(db, "overall"),
            "best_dressed": rank.podium(db, "best_dressed"),
            "couple": rank.podium(db, "couple"),
            "candid": rank.podium(db, "candid"),
        },
        "cover": rank.cover_of_the_day(db, day_start),
        "honor_roll": [dict(r) for r in db.execute(
            "SELECT uploader, badge FROM badges ORDER BY created_at DESC LIMIT 40")],
        "mystery": [dict(r) for r in db.execute(
            "SELECT id, cover_photo, face_count FROM people WHERE name IS NULL OR name = ''"
            " ORDER BY face_count DESC LIMIT 12")],
    }


@app.post("/api/applaud")
async def applaud(request: Request):
    """Family rates each other's photos. Applauds count toward the score,
    so the crowd has a real say in who takes the crown."""
    body = await request.json()
    if not check_token(body.get("t", "")):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    db.execute("INSERT OR IGNORE INTO applause VALUES (?,?,?)",
               (body["photo_id"], (body.get("author") or "Guest")[:40], time.time()))
    db.commit()
    score, reason = rank.rescore(db, body["photo_id"])
    return {"ok": True, "score": score, "reason": reason,
            "applause": rank._applause(db, body["photo_id"])}


@app.post("/api/people/{person_id}/name")
async def name_person(person_id: str, request: Request):
    body = await request.json()
    if not check_token(body.get("t", "")):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    db.execute("UPDATE people SET name = ? WHERE id = ?", (body.get("name", "")[:60], person_id))
    db.commit()
    return {"ok": True}


@app.post("/api/photos/{photo_id}/comment")
async def comment(photo_id: str, request: Request):
    body = await request.json()
    if not check_token(body.get("t", "")):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    db.execute("INSERT INTO comments (photo_id, author, text, created_at) VALUES (?,?,?,?)",
               (photo_id, (body.get("author") or "Guest")[:40], body.get("text", "")[:500], time.time()))
    db.commit()
    return {"ok": True}


@app.get("/api/photos/{photo_id}/comments")
def comments(photo_id: str, t: str = ""):
    if not check_token(t):
        return JSONResponse({"error": "bad link"}, status_code=403)
    db = store.connect()
    return {"comments": [dict(r) for r in db.execute(
        "SELECT author, text, created_at FROM comments WHERE photo_id = ? ORDER BY created_at", (photo_id,))]}


@app.get("/media/{photo_id}")
def media(photo_id: str, dl: int = 0):
    db = store.connect()
    row = db.execute("SELECT filename, orig_name FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(os.path.join(store.MEDIA_DIR, row["filename"]),
                        filename=(row["orig_name"] or row["filename"]) if dl else None)


@app.get("/thumb/{photo_id}")
def thumb(photo_id: str):
    db = store.connect()
    row = db.execute("SELECT filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        return PlainTextResponse("not found", status_code=404)
    tpath = os.path.join(store.THUMB_DIR, row["filename"] + ".jpg")
    if not os.path.exists(tpath):
        try:
            with Image.open(os.path.join(store.MEDIA_DIR, row["filename"])) as im:
                im.thumbnail((480, 480))
                im.convert("RGB").save(tpath, "JPEG", quality=82)
        except Exception:
            return FileResponse(os.path.join(store.MEDIA_DIR, row["filename"]))
    return FileResponse(tpath)


# ---------------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(f'<meta http-equiv="refresh" content="0;url=/g/{quote(wa.GALLERY_TOKEN, safe="/+=")}">')


def _options(labels):
    return "".join(f'<option value="{k}"{" selected" if k == "wedding" else ""}>{escape(label)}</option>'
                   for k, label in labels)


def _render(tpl, **kw):
    """Fill the page placeholders: couple names, tagline, functions, plus extras."""
    import json
    values = {"__BRIDE__": escape(config.BRIDE), "__GROOM__": escape(config.GROOM),
              "__COUPLE__": escape(config.COUPLE), "__TAGLINE__": escape(config.TAGLINE),
              "__EVENTS_JSON__": json.dumps([["", "All"]] + config.EVENT_LABELS),
              "__EVENT_OPTIONS__": _options(config.LIVE_LABELS),
              "__UPLOAD_OPTIONS__": _options(config.UPLOAD_LABELS),
              "__CONFETTI__": CONFETTI_JS}
    values.update(kw)
    for k, v in values.items():
        tpl = tpl.replace(k, v)
    return tpl


def _og(title, desc, image, url):
    return (f'<meta property="og:type" content="website"><meta property="og:title" content="{escape(title)}">'
            f'<meta property="og:description" content="{escape(desc)}">'
            f'<meta property="og:image" content="{escape(image)}"><meta property="og:url" content="{escape(url)}">'
            '<meta name="twitter:card" content="summary_large_image">')


def _origin(request: Request):
    return wa.BASE_URL or str(request.base_url).rstrip("/")


@app.get("/g/{token:path}", response_class=HTMLResponse)   # :path — tokens may contain "/"
def gallery(token: str, request: Request, person: str = ""):
    if not check_token(token):
        return HTMLResponse("This link is not valid.", status_code=403)
    db = store.connect()
    origin = _origin(request)
    cover = None
    title, desc = f"{config.COUPLE} — Wedding Album 💛", "Every photo from the wedding — view, applaud and share."
    if person:
        pr = db.execute("SELECT name, cover_photo FROM people WHERE id = ?", (person,)).fetchone()
        if pr:
            cover = pr["cover_photo"]
            title = f"{pr['name'] or 'A mystery guest'} at {config.COUPLE}'s wedding 💛"
            desc = "Every photo of them from the family album."
    if not cover:
        _, day_start, _ = _day()
        c = rank.cover_of_the_day(db, day_start)
        cover = c["id"] if c else None
    og = _og(title, desc, f"{origin}/thumb/{cover}", f"{origin}{request.url.path}") if cover else ""
    return HTMLResponse(_render(GALLERY_HTML, __TOKEN__=token, __OG__=og))


@app.get("/p/{photo_id}", response_class=HTMLResponse)
def photo_page(photo_id: str, request: Request):
    """Single-photo page for sharing. Needs no album token, so passing one
    photo along never reveals the rest of the album."""
    db = store.connect()
    row = db.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
    if not row:
        return HTMLResponse("Photo not found.", status_code=404)
    origin = _origin(request)
    name = row["uploader"] or "family"
    if re.fullmatch(r"Guest [0-9A-F]{4}", name):
        name = "a guest"
    who = escape(name)
    cap = escape(row["caption"] or "")
    if row["event"] == config.MEMORY:
        cap = escape(f"📜 Memory Lane{' · ' + row['memory_year'] if row['memory_year'] else ''}") + ("<br>" + cap if cap else "")
    og = _og(f"Photo by {name} 💛", row["caption"] or f"From {config.COUPLE}'s wedding",
             f"{origin}/thumb/{photo_id}", f"{origin}/p/{photo_id}")
    return HTMLResponse(_render(PHOTO_HTML, __OG__=og, __ID__=photo_id, __WHO__=who, __CAP__=cap))


@app.get("/tv/{token:path}", response_class=HTMLResponse)
def tv_page(token: str):
    if not check_token(token):
        return HTMLResponse("This link is not valid.", status_code=403)
    return HTMLResponse(_render(TV_HTML, __TOKEN__=token))


@app.get("/upload", response_class=HTMLResponse)
def upload_page(t: str = ""):
    if not check_token(t):
        return HTMLResponse("This link is not valid.", status_code=403)
    return HTMLResponse(_render(UPLOAD_HTML, __TOKEN__=t))


GALLERY_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__COUPLE__ 💛</title>__OG__<style>
body{font-family:system-ui;margin:0;background:#1a1210;color:#fff}
header{padding:14px 16px;background:linear-gradient(90deg,#7c2d12,#b45309);position:sticky;top:0;z-index:5;border-bottom:3px solid #f59e0b}
h1{font-size:20px;margin:0} .sub{opacity:.85;font-size:13px}
.bar{display:flex;gap:8px;overflow-x:auto;padding:10px 12px;background:#241612}
.chip{white-space:nowrap;padding:7px 13px;border-radius:99px;background:#3b241c;color:#ffd9a0;border:1px solid #6b4226;font-size:14px;cursor:pointer}
.chip.on{background:#f59e0b;color:#1a1210;font-weight:600}
#q{width:calc(100% - 24px);margin:10px 12px 0;padding:11px;border-radius:10px;border:1px solid #6b4226;background:#241612;color:#fff;font-size:16px}
#people{display:flex;gap:10px;overflow-x:auto;padding:12px}
.person{text-align:center;cursor:pointer;font-size:12px;width:64px}
.person img{width:56px;height:56px;border-radius:50%;object-fit:cover;border:2px solid #f59e0b}
#grid{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;padding:3px}
#grid img{width:100%;aspect-ratio:1;object-fit:cover;cursor:pointer}
#lb{position:fixed;inset:0;background:#000;display:none;flex-direction:column;z-index:9}
#lb img{flex:1;object-fit:contain;width:100%}
#lb .row{display:flex;gap:8px;padding:12px;justify-content:space-between;align-items:center}
button.act{background:#f59e0b;border:0;border-radius:8px;padding:10px 14px;font-size:15px;font-weight:600;cursor:pointer}
a.act{background:#25d366;color:#06281a;text-decoration:none;border-radius:8px;padding:10px 14px;font-weight:600}
#cap{padding:0 14px 8px;font-size:14px;opacity:.9}
#cover img{width:100%;max-height:280px;object-fit:cover}
#cover .tag{position:absolute;bottom:8px;left:12px;background:#000000aa;padding:6px 12px;border-radius:8px;font-size:14px}
#cover{position:relative}
#podiums{display:flex;gap:10px;overflow-x:auto;padding:12px}
.podium{min-width:200px;background:#241612;border:1px solid #6b4226;border-radius:12px;overflow:hidden}
.podium img{width:100%;aspect-ratio:4/3;object-fit:cover;cursor:pointer}
.podium .body{padding:8px 10px;font-size:13px}
.podium .why{opacity:.85;font-size:12px;margin-top:4px}
#honor{overflow-x:auto;white-space:nowrap;padding:4px 12px;font-size:13px}
.badge{display:inline-block;background:#3b241c;border:1px solid #f59e0b;border-radius:99px;padding:5px 11px;margin:3px;font-size:12px}
#mystery{display:flex;gap:10px;overflow-x:auto;padding:6px 12px 20px}
.mys{text-align:center;width:110px;flex-shrink:0}
.mys img{width:88px;height:88px;border-radius:50%;object-fit:cover;border:3px dashed #f59e0b}
.mys input{width:90px;margin-top:6px;padding:6px;border-radius:8px;border:1px solid #6b4226;background:#241612;color:#fff;font-size:13px}
.claprow{display:flex;gap:8px;align-items:center}
#lb .row{flex-wrap:wrap;justify-content:center}
#lb .row .act{padding:9px 12px;font-size:14px}
a.dl{background:#3b241c;color:#ffd9a0;text-decoration:none;border-radius:8px;padding:9px 12px;font-weight:600;font-size:14px}
#toast{position:fixed;left:50%;top:70px;transform:translate(-50%,-30px);background:#f59e0b;color:#1a1210;font-weight:700;
 padding:11px 18px;border-radius:99px;z-index:20;opacity:0;transition:.35s;pointer-events:none;max-width:90vw;text-align:center}
#toast.on{opacity:1;transform:translate(-50%,0)}
.tools{display:flex;gap:8px;flex-wrap:wrap;padding:0 12px 10px}
a.tool{display:inline-block;background:#3b241c;color:#ffd9a0;border:1px solid #6b4226;border-radius:8px;padding:10px 14px;font-weight:600;text-decoration:none;font-size:15px;cursor:pointer}
header a{color:#fff;font-size:13px;opacity:.9}
body{padding-bottom:84px}
#fab{position:fixed;right:16px;bottom:18px;z-index:8;background:#f59e0b;color:#1a1210;border:0;border-radius:99px;
 padding:15px 22px;font-size:17px;font-weight:800;box-shadow:0 6px 20px #000a;cursor:pointer}
#up{position:fixed;inset:0;background:#000c;display:none;align-items:flex-end;justify-content:center;z-index:15}
#up .card{background:#241612;border:1px solid #6b4226;border-radius:18px 18px 0 0;padding:20px;width:100%;max-width:480px;box-sizing:border-box}
#up h3{margin:0 0 4px;font-size:19px} #up label{display:block;margin:14px 0 6px;font-weight:600;font-size:14px}
#up select,#up input[type=file]{width:100%;padding:12px;font-size:16px;border-radius:10px;border:1px solid #6b4226;background:#1a1210;color:#fff;box-sizing:border-box}
#up .btns{display:flex;gap:8px;margin-top:16px} #up .btns .act{flex:1;padding:13px;font-size:16px}
#upmsg{margin-top:14px;font-size:15px;line-height:1.5}
#up input[type=text]{width:100%;padding:12px;font-size:16px;border-radius:10px;border:1px solid #6b4226;background:#1a1210;color:#fff;box-sizing:border-box}
.chk{display:flex;gap:10px;align-items:flex-start;margin-top:14px;padding:12px;border:1px dashed #f59e0b;border-radius:10px;font-size:14px;line-height:1.35;cursor:pointer}
.chk input{width:20px;height:20px;margin:0;flex-shrink:0}
#memhint{display:none;margin:0 12px 10px;padding:12px 14px;background:#3b241c;border:1px solid #6b4226;border-radius:12px;font-size:14px;line-height:1.45}
.yr{position:relative;display:block}.yr img{display:block}
.yr b{position:absolute;left:4px;bottom:4px;background:#000b;color:#fde68a;border-radius:6px;padding:2px 7px;font-size:12px}
</style></head><body>
<header><h1>🪔 __BRIDE__ ♥ __GROOM__</h1><div class="sub">__TAGLINE__</div><div class="sub"><span id="count"></span> · <a href="/tv/__TOKEN__" target="_blank">📺 TV mode</a></div></header>
<div id="toast"></div>
<button id="fab" onclick="openUp()">📸 Add photos</button>
<div id="up" onclick="if(event.target===this)closeUp()"><div class="card" autocomplete="off">
<h3>📸 Add your photos</h3><div style="opacity:.8;font-size:14px">Full quality — they appear in the album right away.</div>
<label>What are you sharing?</label><select id="upev" onchange="memToggle()">__UPLOAD_OPTIONS__</select>
<div id="memfields" style="display:none"><label>Which year? <small style="opacity:.7">(optional)</small></label>
<input type="text" id="upyr" inputmode="numeric" maxlength="4" placeholder="e.g. 2015">
<label>What's the story? <small style="opacity:.7">(optional)</small></label>
<input type="text" id="upstory" maxlength="200" placeholder="e.g. Ravi Mama's wedding, Hyderabad"></div>
<label>Photos</label><input type="file" id="upfiles" accept="image/*" multiple>
<div class="btns"><button class="act" id="upgo" onclick="doUpload()">Upload</button>
<button class="act" style="background:#3b241c;color:#ffd9a0" onclick="closeUp()">Close</button></div>
<div id="upmsg"></div></div></div>
<div id="cover"></div>
<div class="bar" id="events"></div>
<div style="padding:10px 12px 0;font-size:18px;font-weight:700">🏆 Leaderboard <small style="font-weight:400;opacity:.75">updates the second anyone uploads!</small></div>
<div class="bar" id="cats"></div>
<div id="podiums"></div>
<div id="honor"></div>
<input id="q" placeholder="Search: dancing, dhol, Deepu, Krishna, mehndi…">
<div id="people"></div>
<div id="memhint"></div>
<div class="tools"><button class="act" onclick="slideshow()">▶ Play slideshow</button>
<a class="tool" id="mine" onclick="toggleMine()" style="display:none">🙋 My photos</a>
<a class="tool" id="zip" href="#">⬇ Download these (zip)</a>
<a class="tool" id="sharep" onclick="sharePerson()" style="display:none">🔗 Share this person</a></div>
<div id="grid"></div>
<div id="guesswho" style="display:none"><div style="padding:10px 12px;font-size:18px;font-weight:700">❓ Guess Who <small style="font-weight:400;opacity:.75">name the mystery guests — their photos unlock!</small></div>
<div id="mystery"></div></div>
<div id="lb"><div class="row"><button class="act" onclick="closeLb()">✕ Close</button>
<button class="act" id="clapbtn">👏 Applaud (<span id="clapn">0</span>)</button>
<button class="act" id="sharebtn" onclick="nativeShare()">↗ Share</button>
<a class="act" id="washare" target="_blank">WhatsApp</a>
<a class="dl" id="dl" download>⬇ Save</a></div>
<img id="lbimg"><div id="cap"></div></div>
<script>
__CONFETTI__
const T="__TOKEN__"; let photos=[], cur=0, timer=null, shareFile=null, uploaderF="";
const getMe=()=>localStorage.getItem("wa_name")||"";
const disp=u=>!u?'family':/^Guest [0-9A-F]{4}$/.test(u)?'a guest':u;
function guestId(){let g=getMe();if(!g){g='Guest '+Math.random().toString(16).slice(2,6).toUpperCase();localStorage.setItem("wa_name",g)}return g}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('on');
  clearTimeout(window._t);window._t=setTimeout(()=>t.classList.remove('on'),3500)}
const EV=__EVENTS_JSON__;
const CATS=[["overall","👑 Top 3"],["best_dressed","🥻 Best Dressed"],["couple","💍 __BRIDE__ & __GROOM__"],["candid","😂 Candid Fun"]];
let ev="", person=new URLSearchParams(location.search).get("person")||"", cat="overall", top3={};
document.getElementById('clapbtn').onclick=async()=>{
  const p=photos[cur]; const author=guestId();
  const d=await (await fetch("/api/applaud",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({t:T,photo_id:p.id,author})})).json();
  document.getElementById('clapn').textContent=d.applause; loadTop3();
};
async function nameFace(id){
  const name=document.getElementById('nm_'+id).value.trim(); if(!name)return;
  await fetch(`/api/people/${id}/name`,{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({t:T,name})});
  alert(`🎉 Mystery solved — that's ${name}!`); loadTop3();
}
async function loadTop3(){
  const d=await (await fetch(`/api/top3?t=${T}`)).json(); top3=d;
  const c=top3.cover;
  document.getElementById('cover').innerHTML=c?`<img src="/thumb/${c.id}" onclick="openLbById('${c.id}')">
    <div class="tag">📰 Cover of the Day — ${disp(c.uploader)}</div>`:'';
  const shownCats=CATS.filter(([k])=>k==='overall'||(top3.podiums[k]||[]).length);
  if(!shownCats.some(([k])=>k===cat))cat='overall';
  document.getElementById('cats').innerHTML=shownCats.map(([k,v])=>`<span class="chip ${cat===k?'on':''}" onclick="cat='${k}';loadTop3()">${v}</span>`).join("");
  const list=(top3.podiums[cat]||[]);
  document.getElementById('podiums').innerHTML=list.length?list.map(p=>`<div class="podium">
    <img src="/thumb/${p.id}" onclick="openLbById('${p.id}')">
    <div class="body"><b>${p.medal} ${p.applause||0} 👏</b><div class="why">${p.reason||''}</div></div></div>`).join("")
    :'<div style="padding:12px;opacity:.7">No contenders yet — upload a photo and take the crown! 👑</div>';
  document.getElementById('honor').innerHTML=top3.honor_roll.length
    ? '🏅 <b>Honor Roll:</b> ' + top3.honor_roll.map(h=>`<span class="badge">${h.badge} — ${h.uploader}</span>`).join("")
    : '';
  document.getElementById('mystery').innerHTML=top3.mystery.length
    ? top3.mystery.map(m=>`<div class="mys"><img src="/thumb/${m.cover_photo}"><div style="font-size:12px">in ${m.face_count} photos</div>
       <input id="nm_${m.id}" placeholder="Who?"><br><button class="act" style="margin-top:4px;padding:5px 10px;font-size:13px" onclick="nameFace('${m.id}')">Say name</button></div>`).join("")
    : '';
  document.getElementById('guesswho').style.display=top3.mystery.length?'':'none';   // no faces found yet -> no empty game
}
function openLbById(id){const i=photos.findIndex(p=>p.id===id);openLb(i>=0?i:0)}
function filterQS(){return `event=${ev}&person=${person}&uploader=${encodeURIComponent(uploaderF)}&q=${encodeURIComponent(document.getElementById('q').value)}`}
async function load(){
  const lbOpen=document.getElementById('lb').style.display==='flex', curId=photos[cur]&&photos[cur].id;
  const d=await (await fetch(`/api/photos?t=${T}&${filterQS()}`)).json(); photos=d.photos;
  if(lbOpen&&curId){const i=photos.findIndex(p=>p.id===curId);cur=i>=0?i:0}
  document.getElementById('zip').href=`/api/download?t=${T}&${filterQS()}`;
  const mine=document.getElementById('mine');mine.style.display=getMe()?'':'none';
  mine.style.background=uploaderF?'#f59e0b':'';mine.style.color=uploaderF?'#1a1210':'';
  document.getElementById('sharep').style.display=person?'':'none';
  document.getElementById('count').textContent=ev==='throwback'?photos.length+" memories treasured 📜":photos.length+" photos shared with blessings 🙏";
  const mem=ev==='throwback';
  if(mem){photos.sort((a,b)=>(a.memory_year||'9999').localeCompare(b.memory_year||'9999')||a.created_at-b.created_at); // oldest first, like a timeline
    if(lbOpen&&curId){const i=photos.findIndex(p=>p.id===curId);cur=i>=0?i:0}}
  const hint=document.getElementById('memhint');hint.style.display=mem?'block':'none';
  hint.innerHTML='📜 <b>Memory Lane</b> — photos from past gatherings and earlier weddings, oldest first. Tap <b>Add photos</b> and tick “old memory” to share yours.';
  document.getElementById('grid').innerHTML=photos.map((p,i)=>mem
    ?`<span class="yr" onclick="openLb(${i})"><img src="/thumb/${p.id}" loading="lazy">${p.memory_year?`<b>${p.memory_year}</b>`:''}</span>`
    :`<img src="/thumb/${p.id}" onclick="openLb(${i})" loading="lazy">`).join("");
  if(mem&&!photos.length)document.getElementById('grid').innerHTML='<div style="padding:20px;opacity:.75;grid-column:1/-1">No memories yet — be the first to share one! 📜</div>';
  document.getElementById('people').style.display=d.people.length?'':'none';
  document.getElementById('q').placeholder=d.smart_tags?'Search: dancing, dhol, Deepu, Krishna, mehndi…':'Search by caption or who shared it…';
  document.getElementById('people').innerHTML=d.people.map(p=>`<div class="person" onclick="setPerson('${p.id}')">
    <img src="/thumb/${p.cover_photo||''}"><br>${p.name||'Who is this?'}<br><small>${p.face_count} photos</small></div>`).join("");
  document.getElementById('events').innerHTML=EV.map(([k,v])=>`<span class="chip ${ev===k?'on':''}" onclick="setEv('${k}')">${v}${d.event_counts[k]?' ('+d.event_counts[k]+')':''}</span>`).join("");
}
function setEv(k){ev=k;person="";load()} function setPerson(id){person=person===id?"":id;load()}
function toggleMine(){uploaderF=uploaderF?"":getMe();load()}
async function sharePerson(){const url=`${location.origin}/g/${T}?person=${person}`;
  if(navigator.share){try{await navigator.share({title:'Wedding photos',text:'All their photos from the wedding 💛',url});return}catch(e){if(e.name==='AbortError')return}}
  window.open('https://wa.me/?text='+encodeURIComponent(url),'_blank')}
document.getElementById('q').addEventListener('input',()=>{clearTimeout(window._d);window._d=setTimeout(load,350)});
function openLb(i){cur=i;const p=photos[i];if(!p)return;document.getElementById('lb').style.display='flex';
  document.getElementById('lbimg').src='/media/'+p.id;
  document.getElementById('clapn').textContent=p.applause||0;
  document.getElementById('cap').textContent=p.event==='throwback'?`📜 ${p.memory_year?p.memory_year+' — ':''}${p.caption||'A treasured memory'}  ·  shared by ${disp(p.uploader)}`:(p.caption||'')+`  ·  shared by ${disp(p.uploader)}  ·  score ${Math.round(p.score||0)}`;
  document.getElementById('washare').href='https://wa.me/?text='+encodeURIComponent('From the wedding 💛 '+location.origin+'/p/'+p.id);
  document.getElementById('dl').href='/media/'+p.id+'?dl=1';
  shareFile=null;fetch('/media/'+p.id).then(r=>r.blob()).then(b=>{
    if(photos[cur]&&photos[cur].id===p.id)shareFile=new File([b],p.orig_name||(p.id+'.jpg'),{type:b.type||'image/jpeg'})}).catch(()=>{});}
async function nativeShare(){
  const p=photos[cur];if(!p)return;const url=location.origin+'/p/'+p.id;
  try{ if(shareFile&&navigator.canShare&&navigator.canShare({files:[shareFile]})){
        await navigator.share({files:[shareFile],text:'From the wedding 💛 '+url});return}
       if(navigator.share){await navigator.share({title:'Wedding photo',text:'From the wedding 💛',url});return}
  }catch(e){if(e.name==='AbortError')return}
  try{await navigator.clipboard.writeText(url);toast('🔗 Link copied — paste it anywhere')}catch(e){prompt('Copy this link:',url)}}
function closeLb(){document.getElementById('lb').style.display='none';stopShow()}
function slideshow(){if(!photos.length)return;openLb(0);timer=setInterval(()=>{cur=(cur+1)%photos.length;openLb(cur)},3500)}
function stopShow(){if(timer)clearInterval(timer);timer=null}
document.getElementById('lbimg').addEventListener('click',e=>{const r=e.target.getBoundingClientRect();
  cur=(cur+(e.clientX>r.left+r.width/2?1:-1)+photos.length)%photos.length;openLb(cur)});
function memToggle(){document.getElementById('memfields').style.display=document.getElementById('upev').value==='throwback'?'':'none'}
function openUp(){const sel=document.getElementById('upev'); // start on the tab you're looking at, never a stale choice
  sel.value=[...sel.options].some(o=>o.value===ev&&ev)?ev:'wedding';memToggle();
  document.getElementById('upfiles').value='';document.getElementById('upmsg').innerHTML='';document.getElementById('up').style.display='flex'}
function closeUp(){document.getElementById('up').style.display='none'}
async function doUpload(){
  const msg=document.getElementById('upmsg'), inp=document.getElementById('upfiles'), go=document.getElementById('upgo');
  const files=[...inp.files]; if(!files.length){msg.textContent='Pick some photos first 🙂';return}
  const evSel=document.getElementById('upev').value, isMem=evSel==='throwback', gid=guestId(), BATCH=4; // small batches keep phone uploads reliable
  let ok=0,best=null,badges=[],crown=false; go.disabled=true;
  for(let i=0;i<files.length;i+=BATCH){
    const fd=new FormData();fd.append('t',T);fd.append('uploader',gid);fd.append('event',evSel);
    if(isMem){fd.append('year',document.getElementById('upyr').value);fd.append('story',document.getElementById('upstory').value)}
    files.slice(i,i+BATCH).forEach(f=>fd.append('files',f));
    msg.textContent=`Uploading ${Math.min(i+BATCH,files.length)} of ${files.length}… ⚡`;
    try{const d=await (await fetch('/api/upload',{method:'POST',body:fd})).json();
      (d.results||[]).forEach(r=>{ok++;if(!r.memory&&(!best||r.position<best.position))best=r;badges.push(...r.badges);if(r.crown)crown=true})}catch(e){}
  }
  go.disabled=false;inp.value='';
  if(!ok){msg.textContent='Something went wrong — please try again.';return}
  const head=isMem?`📜 ${ok} memor${ok>1?'ies':'y'} added to Memory Lane — thank you for sharing!`:crown?'👑👑👑 CROWN TAKEN! One of your photos is #1!':best.position<=3?`${['🥇','🥈','🥉'][best.position-1]} On the podium — your best is #${best.position}!`:`✅ ${ok} photo${ok>1?'s':''} added — your best is ranked #${best.position}.`;
  msg.innerHTML=head+(best&&!isMem?`<br><small>${best.reason}</small>`:'')+(badges.length?`<br>🏅 New badge: ${[...new Set(badges)].join(', ')}`:'')+(ok<files.length?`<br>⚠️ ${files.length-ok} file(s) couldn't be added.`:'');
  if(crown)confetti();
  load();loadTop3();
}
let sig=null, lastLeader=null, since=0;
async function pulse(){
  try{const d=await (await fetch(`/api/pulse?t=${T}&since=${since}`)).json();
    const s=d.count+'|'+d.applause+'|'+d.leader;
    if(sig!==null&&s!==sig){
      if(d.new.length){const n=d.new[d.new.length-1];toast(n.event==='throwback'?`📜 New memory from ${disp(n.uploader)}!`:`📸 New photo from ${disp(n.uploader)}!`)}
      load();loadTop3()}
    if(lastLeader!==null&&d.leader&&d.leader!==lastLeader){confetti();toast('👑 A new photo just took the crown!')}
    sig=s;lastLeader=d.leader;since=d.now}catch(e){}}
load(); loadTop3(); pulse(); setInterval(pulse, 5000);
</script></body></html>"""

UPLOAD_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Share your photos 📸</title><style>
body{font-family:system-ui;margin:0;background:#fff8f0;color:#3b241c;padding:20px}
h1{font-size:22px} label{display:block;margin:16px 0 6px;font-weight:600}
input,select{width:100%;padding:13px;font-size:16px;border-radius:10px;border:1px solid #d6a05a;box-sizing:border-box}
button{width:100%;margin-top:22px;padding:15px;font-size:17px;font-weight:700;background:#b45309;color:#fff;border:0;border-radius:10px}
#msg{margin-top:14px;font-size:15px}
</style></head><body>
<h1>📸 Share photos from __BRIDE__ &amp; __GROOM__'s wedding 🪔</h1>
<p>Pick photos from your phone or camera roll — full quality, no compression. They'll appear in the family album for everyone.</p>
<form id="f">
<label>Which function?</label><select name="event">
__EVENT_OPTIONS__</select>
<label>Photos</label><input type="file" name="files" accept="image/*" multiple required>
<button type="submit">Upload photos</button><div id="msg"></div></form>
<script>
document.getElementById('f').addEventListener('submit',async e=>{e.preventDefault();
let gid=localStorage.getItem('wa_name');   // anonymous per-phone id: keeps badges and "My photos" working
if(!gid){gid='Guest '+Math.random().toString(16).slice(2,6).toUpperCase();localStorage.setItem('wa_name',gid)}
const fd=new FormData(e.target);fd.append('t','__TOKEN__');fd.append('uploader',gid);
document.getElementById('msg').textContent='Uploading & scoring… ⚡';
const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();
if(!d.results||!d.results.length){document.getElementById('msg').textContent='Something went wrong — please try again.';return}
document.getElementById('msg').innerHTML=d.results.map(res=>
  res.crown?`👑👑👑 CROWN TAKEN! Your photo is #1! ${res.reason}`
  :res.position===2?`🥈 So close — your photo is #2! ${res.reason}`
  :res.position===3?`🥉 On the podium — #3! ${res.reason}`
  :`✅ Ranked #${res.position} — the crown is within reach! ${res.reason}`
  +(res.badges.length?`<br>🏅 New badge: ${res.badges.join(', ')}`:'')).join('<br><br>')
  +'<br><br><a href="/g/__TOKEN__">👉 See the live leaderboard</a>';});
</script></body></html>"""


CONFETTI_JS = r"""
function confetti(n){n=n||140;const c=document.createElement('canvas'); // phoolon ki barish: a shower of petals
  c.style.cssText='position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:99';
  document.body.appendChild(c);const x=c.getContext('2d');c.width=innerWidth;c.height=innerHeight;
  const cols=['#f59e0b','#fbbf24','#ea580c','#f97316','#e11d48','#fb7185','#fde68a','#d97706'];
  const ps=Array.from({length:n},()=>({x:Math.random()*c.width,y:-20-Math.random()*c.height*.4,
    vx:(Math.random()-.5)*3,vy:1.5+Math.random()*3,s:6+Math.random()*8,r:Math.random()*6,vr:(Math.random()-.5)*.2,
    sw:Math.random()*6,col:cols[(Math.random()*cols.length)|0]}));
  let f=0;(function tick(){x.clearRect(0,0,c.width,c.height);
    ps.forEach(p=>{p.sw+=.06;p.x+=p.vx+Math.sin(p.sw)*1.2;p.y+=p.vy;p.r+=p.vr;x.save();x.translate(p.x,p.y);x.rotate(p.r);
      x.fillStyle=p.col;x.beginPath();x.ellipse(0,0,p.s,p.s*.5,0,0,6.283);x.fill();
      x.fillStyle='#ffffff33';x.beginPath();x.ellipse(-p.s*.2,0,p.s*.5,p.s*.18,0,0,6.283);x.fill();x.restore()});
    if(++f<300)requestAnimationFrame(tick);else c.remove()})()}
"""

PHOTO_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Wedding photo 💛</title>__OG__
<style>body{margin:0;background:#1a1210;color:#fff;font-family:system-ui;text-align:center}
img{max-width:100%;max-height:78vh;object-fit:contain}.meta{padding:12px 16px;font-size:15px;opacity:.9}
.act{display:inline-block;background:#f59e0b;color:#1a1210;border:0;border-radius:8px;padding:11px 16px;font-size:15px;
font-weight:700;text-decoration:none;margin:4px;cursor:pointer}</style></head><body>
<img src="/media/__ID__"><div class="meta">📸 Shared by <b>__WHO__</b><br>__CAP__</div>
<a class="act" href="/media/__ID__?dl=1" download>⬇ Save photo</a>
<button class="act" id="s">↗ Share</button>
<script>document.getElementById('s').onclick=async()=>{const url=location.href;
try{const b=await (await fetch('/media/__ID__')).blob();const f=new File([b],'wedding-photo.jpg',{type:b.type||'image/jpeg'});
if(navigator.canShare&&navigator.canShare({files:[f]})){await navigator.share({files:[f],text:'From the wedding 💛 '+url});return}
if(navigator.share){await navigator.share({url});return}}catch(e){if(e.name==='AbortError')return}
try{await navigator.clipboard.writeText(url);alert('Link copied')}catch(e){prompt('Copy this link:',url)}}</script>
</body></html>"""

TV_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__COUPLE__ 📺</title><style>
html,body{margin:0;height:100%;background:#000;color:#fff;font-family:system-ui;overflow:hidden;cursor:none}
.layer{position:absolute;inset:0;background-size:contain;background-position:center;background-repeat:no-repeat;opacity:0;transition:opacity 1.2s}
.layer.on{opacity:1}
#bg{position:absolute;inset:0;filter:blur(40px) brightness(.45);background-size:cover;background-position:center;transform:scale(1.15)}
#cap{position:absolute;left:3vw;bottom:5vh;font-size:3.2vh;background:#000a;padding:1.2vh 2vw;border-radius:99px}
#title{position:absolute;left:3vw;top:3vh;font-size:4.5vh;font-weight:800;color:#fde68a;text-shadow:0 0 12px #000;display:flex;flex-direction:column}
#title small{font-size:2.4vh;font-weight:500}
#new{position:absolute;left:3vw;top:14vh;font-size:4vh;font-weight:800;background:#f59e0b;color:#1a1210;padding:1vh 2vw;border-radius:99px;display:none}
#top{position:absolute;right:2vw;top:3vh;display:flex;flex-direction:column;gap:1.5vh}
.pod{display:flex;align-items:center;gap:1vw;background:#000b;border:2px solid #f59e0b;border-radius:2vh;padding:.8vh 1.2vw;font-size:2.4vh}
.pod img{height:9vh;width:12vh;object-fit:cover;border-radius:1vh}
#hdr{font-size:2.4vh;text-align:right;color:#fde68a;font-weight:700}
#join{position:absolute;right:2vw;bottom:3vh;font-size:2.4vh;background:#000b;padding:1vh 1.5vw;border-radius:2vh;max-width:40vw;text-align:right}
#crown{position:absolute;inset:0;display:none;align-items:center;justify-content:center;font-size:9vh;font-weight:800;
 text-shadow:0 0 30px #f59e0b;background:#000a;text-align:center;z-index:50}
</style></head><body>
<div id="bg"></div><div class="layer" id="a"></div><div class="layer" id="b"></div>
<div id="title">🪔 __BRIDE__ ♥ __GROOM__ 🪔<small>__TAGLINE__</small></div><div id="new">📸 JUST IN!</div><div id="cap"></div>
<div id="top"><div id="hdr">👑 Top 3 right now</div><div id="pods"></div></div>
<div id="join">📱 Add yours: send photos to our WhatsApp number or open<br><b id="url"></b></div>
<div id="crown">👑 NEW #1!<br><small style="font-size:4vh">A photo just took the crown</small></div>
__CONFETTI__
<script>
const T="__TOKEN__", SHOW_MS=6000; let queue=[], memQ=[], mi=0, slides=0, seen=new Set(), idx=0, front=true, since=0, leader=null, fresh=[];
document.getElementById('url').textContent=location.host+'/upload';
const L={a:document.getElementById('a'),b:document.getElementById('b')};
function show(p,mode){const el=front?L.a:L.b, old=front?L.b:L.a; front=!front;
  const u='/media/'+p.id; const im=new Image(); im.onload=()=>{el.style.backgroundImage=`url(${u})`;
    document.getElementById('bg').style.backgroundImage=`url(${u})`;
    el.classList.add('on');old.classList.remove('on')}; im.src=u;
  const who=p.uploader, name=!who||/^Guest [0-9A-F]{4}$/.test(who)?'a guest':who, nb=document.getElementById('new');
  document.getElementById('cap').textContent=p.event==='throwback'
    ?`📜 ${p.memory_year?p.memory_year+' — ':''}${p.caption||'A treasured memory'} · shared by ${name}`
    :'📸 '+name+(p.caption?' — '+p.caption:'');
  nb.textContent=mode==='mem'?'📜 MEMORY LANE':'📸 JUST IN!';nb.style.display=mode?'block':'none';}
function next(){ // newly arrived photos jump the queue
  if(fresh.length){show(fresh.shift(),'new');return}
  if(memQ.length&&++slides%5===0){show(memQ[mi++%memQ.length],'mem');return}  // every 5th slide: a throwback
  if(!queue.length)return; show(queue[idx%queue.length],false); idx++}
async function loadAll(){
  const m=await (await fetch(`/api/photos?t=${T}&event=throwback&limit=500`)).json();m.photos.forEach(p=>seen.add(p.id));memQ=m.photos;
  const d=await (await fetch(`/api/photos?t=${T}&limit=1000`)).json();
  d.photos.forEach(p=>seen.add(p.id));
  const a=d.photos.slice();for(let i=a.length-1;i>0;i--){const j=(Math.random()*(i+1))|0;[a[i],a[j]]=[a[j],a[i]]}queue=a}
async function loadTop(){const d=await (await fetch(`/api/top3?t=${T}`)).json();
  document.getElementById('pods').innerHTML=(d.podiums.overall||[]).map(p=>`<div class="pod"><img src="/thumb/${p.id}"><div><b>${p.medal}</b> ${p.applause||0} 👏</div></div>`).join('')}
async function pulse(){try{const d=await (await fetch(`/api/pulse?t=${T}&since=${since}`)).json();
  if(since){d.new.forEach(p=>{if(!seen.has(p.id)){seen.add(p.id);if(p.event==='throwback')memQ.push(p);else{fresh.push(p);queue.push(p)}}});
    if(fresh.length&&timerDue()){clearTimeout(tm);tick()}
    if(leader&&d.leader&&d.leader!==leader){confetti(220);const c=document.getElementById('crown');c.style.display='flex';setTimeout(()=>c.style.display='none',5000);loadTop()}
    if(d.new.length)loadTop()}
  leader=d.leader;since=d.now}catch(e){}}
let tm=null, last=0; const timerDue=()=>Date.now()-last>1500;
function tick(){last=Date.now();next();tm=setTimeout(tick,SHOW_MS)}
(async()=>{await loadAll();await loadTop();await pulse();tick();setInterval(pulse,4000);setInterval(loadTop,30000)})();
document.addEventListener('click',()=>document.documentElement.requestFullscreen&&document.documentElement.requestFullscreen());
</script></body></html>"""
