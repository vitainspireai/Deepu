# Wedding Photo Share 💛

Everyone at the family wedding takes pictures. Nobody is in one group,
and nobody wants to install a new app. So: **one WhatsApp number to send
photos to, one link to view them all.**

## Personalising it

Names, tagline and the list of functions live in `app/config.py`
(`BRIDE_NAME`, `GROOM_NAME`, `TAGLINE` can also be set in `.env`). The
functions are Engagement, Haldi, Mehndi, Sangeet, Baraat, Wedding, Vidaai
and Reception — edit the `FUNCTIONS` list to add or remove ceremonies
(e.g. Nalugu, Griha Pravesh), but don't rename a key once photos exist.

## How guests use it

1. **Send photos to the WhatsApp number** — like texting a family member.
   Each photo is acknowledged and lands in the album. Works with zero
   setup; WhatsApp compresses these, so best for quick candids.
2. **Or tap the upload link** shared in the WhatsApp group — pick photos
   from the phone/camera roll, **full resolution, no compression**. Pick
   the function (Haldi, Mehndi, Sangeet, Wedding, Reception) while
   uploading.

## How family views & shares

- **Album link** (`/g/<token>`): mobile-friendly grid, event filters,
  **slideshow play button** ▶, tap any photo to view full-screen and
  **"Share on WhatsApp"** it to anyone — group or not.
- Anyone with the link can view. The token is long and unguessable; rotate
  it (new `GALLERY_TOKEN`) if a link leaks.

## Memory Lane 📜

Guests can also share **old photos** — past family gatherings, earlier
weddings, any treasured moment. In **Add photos**, tick “This is an old
memory”, add the year and a line of story, and upload. Memories live in
their own **Memory Lane** tab (oldest first, like a timeline), appear every
few slides on the TV, and never compete on the wedding leaderboard.
Sharers earn a **Memory Keeper 📜** badge.

## The fun layer 👑 (the bit that fills the album)

Built to turn "please share your photos" into a friendly family contest:

- **Top 3 leaderboard, re-ranked instantly** — every new upload is scored
  the second it lands (sharpness, lighting, faces, AI tags, applause,
  freshness) and the uploader immediately gets their verdict: "🥈 you're
  #2!" or "👑 you just stole the crown!" Exact ties go to the newest
  photo — dethronings should feel dramatic.
- **Playful reasons** — each podium photo carries a generated why-line
  ("Full house! 9 happy faces in one frame 🥳"), so the AI feels like a
  cheeky family commentator, not a judge.
- **👏 Applause** — anyone can applaud any photo from the viewer; applauds
  count toward the score, so the crowd picks winners too. Duplicates and
  blurry shots lose points — send your best, not your burst mode.
- **Category crowns** — separate podiums for **Best Dressed** 💃 (AI
  shortlists the best-shot outfit photos, family votes), **Couple** 💍
  and **Candid Fun** 😂.
- **Dethrone alerts** — when #1 changes hands, the new champion hears
  about it instantly and the old one (via WhatsApp) gets: "knocked off
  the crown — send another and take it back!"
- **Uploader badges** — First Timer, Early Bird, Night Owl, Sharpshooter
  (25 photos), Crowd Magnet, Crown Holder. Shown on a **Honor Roll**
  strip in the album.
- **Guess Who** — unnamed faces appear as a game: name the mystery guest,
  unlock their photos.
- **Cover of the Day** — the day's top photo headlines the album like a
  newspaper front page.

## AI features

- **People grouping**: faces are clustered automatically; each distinct
  face shows as a circle at the top of the album. Family names it once
  ("Bride", "Uncle Ravi") — then one tap shows *all* of that person's
  photos.
- **Smart search**: "dancing", "cake", "baraat", "decorations" —
  auto-tagged so the search box finds moments by description.
- **Quality & duplicates**: blurry shots and duplicates are flagged so
  the slideshow shows the good ones. Duplicates also lose leaderboard
  points (see above) — same photo uploaded twice can't win twice.

Run the AI pass after uploads (e.g. nightly or on demand):

```bash
python -m app.ai_worker
```

Quality/duplicate detection needs only Pillow. Face grouping and smart
tags need `requirements-ai.txt` (torch + insightface — install on the
server, not your laptop).

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in the values
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In Meta's dashboard, create a WhatsApp app (or reuse an existing one) and add a
phone number for this family album:

1. Webhooks → subscribe, callback URL `https://your-host/webhook`,
   verify token = `WA_VERIFY_TOKEN`, subscribe to **messages**.
2. Test: from a phone, send "hi" to the number → you get the links back.

## Honest limits (verified against Meta's platform)

- The WhatsApp number can **receive** photos from anyone — no template
  needed. Replies are free-form as long as the guest messaged first
  (24-hour service window).
- If your app is still in Development mode, only allow-listed numbers can
  *receive* bot replies; incoming webhooks still work.
- **Do not** build this around group-chat monitoring — WhatsApp gives
  bots no access to group messages sent by others. That's why the
  pattern is: guests message the number directly, and the group is only
  for sharing the album/upload links.
- Family photos + face recognition: faces never leave your server in
  this design. Keep it that way if you put it online.
