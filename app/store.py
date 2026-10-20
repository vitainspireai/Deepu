"""SQLite storage for the wedding photo share app.

One file database, no ORM. Tables:
  photos        - every uploaded/received picture
  people        - face clusters found by the AI worker (name filled in by family)
  photo_faces   - which faces (people) appear in which photo
  photo_tags    - AI tags / captions for semantic search
  comments      - lightweight reactions & comments per photo
"""
import os
import sqlite3
import time
import uuid

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
MEDIA_DIR = os.path.join(DATA_DIR, "media")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
DB_PATH = os.path.join(DATA_DIR, "wedding.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    id          TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,          -- stored file name under media/
    orig_name   TEXT,
    source      TEXT NOT NULL,          -- 'whatsapp' | 'web'
    uploader    TEXT,                   -- display name / phone of sender
    caption     TEXT,
    event       TEXT,                   -- haldi, mehndi, sangeet, wedding, reception, other
    taken_at    REAL,                   -- epoch seconds (EXIF or upload time)
    created_at  REAL NOT NULL,
    width       INTEGER,
    height      INTEGER,
    phash       TEXT,
    blur_score  REAL,
    brightness  REAL,
    score       REAL DEFAULT 0,
    ai_done     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS applause (
    photo_id    TEXT NOT NULL,
    author      TEXT,
    created_at  REAL NOT NULL,
    UNIQUE (photo_id, author)
);
CREATE TABLE IF NOT EXISTS badges (
    uploader    TEXT NOT NULL,
    badge       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (uploader, badge)
);
CREATE TABLE IF NOT EXISTS people (
    id          TEXT PRIMARY KEY,
    name        TEXT,                   -- set by family in the gallery ("Bride", "Uncle Ravi"...)
    cover_photo TEXT,
    face_count  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS photo_faces (
    photo_id    TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    PRIMARY KEY (photo_id, person_id)
);
CREATE TABLE IF NOT EXISTS photo_tags (
    photo_id    TEXT NOT NULL,
    tag         TEXT NOT NULL,
    score       REAL,
    PRIMARY KEY (photo_id, tag)
);
CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id    TEXT NOT NULL,
    author      TEXT,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_event ON photos(event);
CREATE INDEX IF NOT EXISTS idx_faces_person ON photo_faces(person_id);
"""


def connect():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(THUMB_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # migrate older databases
    cols = {r["name"] for r in db.execute("PRAGMA table_info(photos)")}
    for col, ctype in (("brightness", "REAL"), ("score", "REAL DEFAULT 0"),
                       ("uploader_wa", "TEXT")):
        if col not in cols:
            db.execute(f"ALTER TABLE photos ADD COLUMN {col} {ctype}")
    db.commit()
    return db


def new_id():
    return uuid.uuid4().hex[:16]


def add_photo(db, *, filename, orig_name, source, uploader, caption=None,
              event=None, taken_at=None, width=None, height=None, uploader_wa=None):
    pid = new_id()
    db.execute(
        "INSERT INTO photos (id, filename, orig_name, source, uploader, caption,"
        " event, taken_at, created_at, width, height, uploader_wa)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, filename, orig_name, source, uploader, caption, event,
         taken_at or time.time(), time.time(), width, height, uploader_wa),
    )
    db.commit()
    return pid
