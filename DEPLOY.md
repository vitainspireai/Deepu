# Deploying the wedding album

You need this running on a server that (a) has a public HTTPS address for
family phones to open, and (b) keeps its files (photos) across restarts.

## Chosen path: Render (~$8 for the wedding month)

This folder is a ready-to-push git repo (secrets and local data excluded
via `.gitignore`). Steps:

```bash
# 1. Create an empty repo on github.com (e.g. wedding-photo-share), then:
git remote add origin https://github.com/<you>/wedding-photo-share.git
git push -u origin main

# 2. Render dashboard → New → Web Service → connect that repo.
#    It reads render.yaml: Starter plan ($7/mo) + 5 GB disk + auto env vars.
#    (If it doesn't pick up render.yaml, set manually: Build =
#    pip install -r requirements.txt, Start =
#    uvicorn app.main:app --host 0.0.0.0 --port $PORT, disk mounted at
#    /opt/render/project/src/data.)

# 3. After the first deploy, in Dashboard → service → Environment:
#    - set BASE_URL to https://<service>.onrender.com (triggers redeploy)
#    - copy the generated GALLERY_TOKEN value

# 4. Family links:
#    Album:  https://<service>.onrender.com/g/<GALLERY_TOKEN>
#    Upload: https://<service>.onrender.com/upload?t=<GALLERY_TOKEN>
```

Cancel/delete the service after the wedding and the bill stops.

## Option A — your own server (best: free, persistent, private)

```bash
# copy the folder to the server, then:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # fill in; generate a long GALLERY_TOKEN
# run it (or use the docker route below):
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &
```

Docker route:

```bash
docker build -t wedding-photos .
docker run -d --restart unless-stopped -p 8000:8000 \
  -e GALLERY_TOKEN=<long-random> -e WA_VERIFY_TOKEN=<random> \
  -v wedding-data:/data --name wedding-photos wedding-photos
```

Put a reverse proxy (nginx/Caddy) in front for HTTPS, or point a
Cloudflare Tunnel at it. Then share:

- Album:   `https://your-domain/g/<GALLERY_TOKEN>`
- Upload:  `https://your-domain/upload?t=<GALLERY_TOKEN>`

## Option B — Render (no server of your own)

1. Push this folder to a GitHub repo.
2. Render dashboard → New → Web Service → connect the repo.
3. It reads `render.yaml` automatically. After deploy, set `BASE_URL` to
   the `https://<service>.onrender.com` address.
4. ⚠️ **Do not use Render's free plan for this**: it has no persistent
   disk, so uploaded photos are deleted whenever the service restarts or
   sleeps. Use Starter or higher (the `render.yaml` already sets that and
   attaches a 5 GB disk).

## WhatsApp photo-drop (either option)

Once the app has a public HTTPS address, point the Meta webhook at
`https://your-domain/webhook` with the same `WA_VERIFY_TOKEN`, subscribe
to `messages`, and fill in `WA_TOKEN` + `WA_PHONE_NUMBER_ID` in `.env`.
Guests then just send photos to the WhatsApp number.

## Night-before checklist

- [ ] Open the album link on your phone — photos load, slideshow plays
- [ ] Upload 2–3 test photos from a guest's phone via the upload link
- [ ] Name the AI-found faces in the People row ("Bride", "Groom"...)
- [ ] Save a copy of the `data/` folder somewhere safe after the wedding
