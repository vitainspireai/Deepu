"""WhatsApp Cloud API helpers (Meta Graph API v21).

Env:
  WA_TOKEN            permanent / test access token
  WA_PHONE_NUMBER_ID  the business number's phone_number_id
  WA_VERIFY_TOKEN     any string you also paste into Meta's webhook setup
  BASE_URL            public https URL of this app (for links sent to guests)
  GALLERY_TOKEN       unguessable token in the public gallery link

Guests never need a template to *receive* a reply: when a guest messages the
number first, the 24-hour service window opens and free-form replies are free.
"""
import os
import requests

GRAPH = "https://graph.facebook.com/v21.0"
TOKEN = os.environ.get("WA_TOKEN", "")
PHONE_ID = os.environ.get("WA_PHONE_NUMBER_ID", "")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
GALLERY_TOKEN = os.environ.get("GALLERY_TOKEN", "")


def gallery_link():
    return f"{BASE_URL}/g/{GALLERY_TOKEN}"


def upload_link():
    return f"{BASE_URL}/upload?t={GALLERY_TOKEN}"


def _headers():
    return {"Authorization": f"Bearer {TOKEN}"}


def send_text(to: str, body: str):
    r = requests.post(
        f"{GRAPH}/{PHONE_ID}/messages",
        headers={**_headers(), "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to,
              "type": "text", "text": {"body": body}},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def download_media(media_id: str, dest_path: str) -> None:
    """Two-step media download: media id -> temporary URL -> bytes."""
    r = requests.get(f"{GRAPH}/{media_id}", headers=_headers(), timeout=30)
    r.raise_for_status()
    url = r.json()["url"]
    with requests.get(url, headers=_headers(), stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)


def profile_name(entry_value: dict, wa_id: str) -> str:
    for c in entry_value.get("contacts", []):
        if c.get("wa_id") == wa_id:
            return (c.get("profile") or {}).get("name") or wa_id
    return wa_id
