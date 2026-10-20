"""Who's getting married, and which functions the album is organised around.

Everything couple- or tradition-specific lives here (override the names and
tagline with env vars) so the rest of the app stays generic.
"""
import os

BRIDE = os.environ.get("BRIDE_NAME", "Deepu")
GROOM = os.environ.get("GROOM_NAME", "Krishna")
COUPLE = f"{BRIDE} & {GROOM}"
TAGLINE = os.environ.get("TAGLINE", "Shubh Vivah 🪔")

# (key, label, playful line shown when a photo from that function scores)
# Keys are stored in the database — add new ones freely, but don't rename
# existing keys once photos have been uploaded.
FUNCTIONS = [
    ("engagement", "Engagement 💍", "Ring-ceremony sparkle 💍"),
    ("haldi", "Haldi 💛", "Haldi glow detected ✨"),
    ("mehndi", "Mehndi 🪔", "Mehndi magic, rachne wali hai 🪔"),
    ("sangeet", "Sangeet 💃", "Sangeet energy is contagious 💃"),
    ("baraat", "Baraat 🥁", "Baraat josh — dhol on full volume 🥁"),
    ("wedding", "Wedding 🔥", "A sacred wedding moment 🪔"),
    ("vidaai", "Vidaai 🥹", "Vidaai — happy tears and blessings 🥹"),
    ("reception", "Reception ✨", "Reception glam 📸"),
    ("other", "More 🌟", "Candid gold 🌟"),
]
# Memory Lane: old photos from past gatherings / earlier weddings. Stored as
# event="throwback" but kept out of the live wedding grid and leaderboard.
MEMORY = "throwback"
MEMORY_LABEL = "Memory Lane 📜"

EVENTS = [k for k, _, _ in FUNCTIONS] + [MEMORY]
EVENT_LINES = {k: line for k, _, line in FUNCTIONS}
LIVE_LABELS = [(k, label) for k, label, _ in FUNCTIONS]          # upload dropdown
EVENT_LABELS = LIVE_LABELS + [(MEMORY, MEMORY_LABEL)]            # album filter chips
UPLOAD_LABELS = LIVE_LABELS + [(MEMORY, "📜 Old memory (past gathering / earlier wedding)")]  # Add-photos dropdown

# Searching a name finds the AI's bride/groom tags too.
SEARCH_ALIASES = {BRIDE.lower(): ["bride"], GROOM.lower(): ["groom"],
                  "dulhan": ["bride"], "dulha": ["groom"],
                  "vadhu": ["bride"], "var": ["groom"]}
