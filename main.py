import os
import re
import time
import json
import statistics
from datetime import datetime
from typing import Dict, Optional, Tuple, List

import pytz
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, JSONResponse

# =========================
# ENV / CONFIG
# =========================
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()   # MUST be phone_number_id, not display number
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()

# Optional APIs for Spot source
GOLDAPI_KEY = os.getenv("GOLDAPI_KEY", "").strip()
EXCHANGERATE_API_KEY = os.getenv("EXCHANGERATE_API_KEY", "").strip()

TZ_JAKARTA = pytz.timezone("Asia/Jakarta")

CACHE_TTL_SECONDS = 300  # 5 minutes
_cache: Dict[str, Tuple[float, object]] = {}

app = FastAPI()


# =========================
# HELPERS
# =========================
def now_wib_str() -> str:
    return datetime.now(TZ_JAKARTA).strftime("%d %b %Y %H:%M WIB")


def cache_get(key: str):
    item = _cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return val


def cache_set(key: str, val: object):
    _cache[key] = (time.time(), val)


def clean_int_from_text(text: str) -> Optional[int]:
    """Extract integer from 'Rp 1.245.000' or '1,245,000'."""
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def normalize_cmd(s: str) -> str:
    """Lowercase and normalize whitespace (handles extra spaces/newlines)."""
    return " ".join((s or "").lower().split())


def rupiah(n: int) -> str:
    # Format 1245000 -> "Rp 1.245.000"
    return "Rp " + f"{n:,}".replace(",", ".")


# =========================
# SOURCES
# =========================

async def fetch_harga_emas_org_spot_per_gram(client: httpx.AsyncClient) -> Optional[int]:
    """
    Extract the big spot-like number from harga-emas.org.
    Tries visible text first, then raw HTML, and supports multiple unit formats.
    """
    url = "https://harga-emas.org/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://harga-emas.org/",

    }

    patterns = [
        r"Rp\s*[\d\.\,]+\s*/\s*g\b",
        r"Rp\s*[\d\.\,]+\s*/\s*gr\b",
        r"Rp\s*[\d\.\,]+\s*/\s*gram\b",
        r"Rp\s*[\d\.\,]+\s*per\s*gram\b",
    ]

    try:
        r = await client.get(url, headers=headers, timeout=20)
        r.raise_for_status()

        html = r.text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)

        # Try visible text first, then raw HTML
        for hay in (text, html):
            for p in patterns:
                m = re.search(p, hay, flags=re.IGNORECASE)
                if m:
                    return clean_int_from_text(m.group(0))
        # Diagnostics: show whether the server response even contains "/g" or "per gram"
        if "/g" not in html.lower() and "per gram" not in html.lower() and "/gr" not in html.lower():
            print("DEBUG harga-emas: no /g text in response (likely JS-rendered or different markup).")
        else:
            # show a small snippet around "/g" if present
            idx = html.lower().find("/g")
            if idx != -1:
                snippet = html[max(0, idx-120): idx+120]
                print("DEBUG harga-emas snippet around /g:", snippet.replace("\n", " ")[:240])

        return None

    except Exception as e:
        print("fetch_harga_emas_org_spot_per_gram error:", repr(e))
        return None



async def fetch_spot_xau_usd_per_oz(client: httpx.AsyncClient) -> Optional[float]:
    """
    Spot XAU/USD USD per troy ounce using GoldAPI (optional).
    If no key, returns None.
    """
    if not GOLDAPI_KEY:
        return None

    url = "https://www.goldapi.io/api/XAU/USD"
    headers = {"x-access-token": GOLDAPI_KEY}

    try:
        r = await client.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()
        price = data.get("price")
        return float(price) if price is not None else None
    except Exception as e:
        print("fetch_spot_xau_usd_per_oz error:", repr(e))
        return None


async def fetch_usd_idr_rate(client: httpx.AsyncClient) -> Optional[float]:
    """
    USD->IDR using exchangerate-api.com (optional).
    Replace with your preferred FX provider if needed.
    """
    if not EXCHANGERATE_API_KEY:
        return None

    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGERATE_API_KEY}/latest/USD"
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        rates = data.get("conversion_rates") or {}
        idr = rates.get("IDR")
        return float(idr) if idr else None
    except Exception as e:
        print("fetch_usd_idr_rate error:", repr(e))
        return None


def xau_usd_oz_to_idr_per_gram(xau_usd_per_oz: float, usd_idr: float) -> int:
    # 1 troy ounce = 31.1034768 grams
    return int(round((xau_usd_per_oz * usd_idr) / 31.1034768))


# =========================
# AGGREGATION
# =========================
def within_pct(a: int, b: int, pct: float) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) / float(b) <= pct


async def get_gold_prices_idr_per_gram() -> Tuple[Dict[str, int], List[str]]:
    cached = cache_get("gold_prices")
    if cached:
        return cached

    prices: Dict[str, int] = {}
    notes: List[str] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # --- SPOT (truth source) ---
        spot_usd = await fetch_spot_xau_usd_per_oz(client)
        fx = await fetch_usd_idr_rate(client)

        spot_idr_g = None
        if spot_usd and fx:
            spot_idr_g = xau_usd_oz_to_idr_per_gram(spot_usd, fx)
            prices["Spot (XAU/USD→IDR)"] = spot_idr_g
        else:
            if GOLDAPI_KEY or EXCHANGERATE_API_KEY:
                notes.append("Spot unavailable (API issue)")
            else:
                notes.append("Spot disabled (no API keys)")

        # --- Harga-Emas.org (/g spot-like) ---
        he = await fetch_harga_emas_org_spot_per_gram(client)
        print("DEBUG harga-emas /g parsed:", he)

        if he:
            if spot_idr_g:
                diff_pct = (he - spot_idr_g) / spot_idr_g * 100
                print(f"DEBUG diff_pct Harga-Emas vs Spot: {diff_pct:.3f}%")

            if spot_idr_g and not within_pct(he, spot_idr_g, 0.03):
                notes.append("Harga-Emas ignored (wrong field / outlier)")
            else:
                prices["Harga-Emas.org (/g)"] = he
        else:
            notes.append("Harga-Emas unavailable")


    cache_set("gold_prices", (prices, notes))
    return prices, notes



def format_price_message(prices: Dict[str, int], notes: List[str]) -> str:
    if not prices:
        return (
            "Maaf, semua sumber harga emas sedang gagal.\n"
            "Coba lagi beberapa menit.\n"
            f"⏱ {now_wib_str()}"
        )

    vals = list(prices.values())
    median = int(statistics.median(vals))
    spread = max(vals) - min(vals) if len(vals) >= 2 else 0

    lines = [
        "💰 Harga Emas (IDR/gram)",
        "──────────────",
    ]
    for k, v in prices.items():
        lines.append(f"{k}: {rupiah(v)}")

    lines.append("──────────────")
    lines.append(f"📊 Median: {rupiah(median)}")
    if len(vals) >= 2:
        lines.append(f"↔️ Perbedaan: {rupiah(spread)}")

    # Keep notes short (max 2)
    if notes:
        lines.append("")
        lines.append("ℹ️ " + " | ".join(notes[:2]))

    lines.append(f"⏱ {now_wib_str()}")
    return "\n".join(lines)


# =========================
# WHATSAPP SEND
# =========================
async def wa_send_text(to: str, body: str) -> None:
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("Missing META_ACCESS_TOKEN or PHONE_NUMBER_ID")
        return

    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }

    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload, timeout=20)
        print("SEND STATUS:", r.status_code)
        print("SEND BODY:", r.text[:800])
        r.raise_for_status()


# =========================
# ROUTES
# =========================
@app.get("/")
async def root():
    return {"ok": True, "service": "whatsapp-gold-bot"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta webhook verification endpoint.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge)

    return PlainTextResponse("OK", status_code=200)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive inbound messages and reply with gold price.
    """
    data = await request.json()
    print("WEBHOOK:", json.dumps(data)[:1200])

    entry = data.get("entry") or []
    if not entry:
        return JSONResponse({"ok": True})

    changes = entry[0].get("changes") or []
    if not changes:
        return JSONResponse({"ok": True})

    value = changes[0].get("value") or {}

    # Ignore status-only webhooks
    messages = value.get("messages") or []
    if not messages:
        return JSONResponse({"ok": True})

    msg = messages[0]
    from_number = msg.get("from")  # digits only
    msg_type = msg.get("type")

    text_body = ""
    if msg_type == "text":
        text_body = ((msg.get("text") or {}).get("body") or "")
    cmd = normalize_cmd(text_body)

    print("FROM:", from_number, "TYPE:", msg_type, "TEXT:", text_body)

    if not from_number:
        return JSONResponse({"ok": True})

    if cmd in ("emas", "gold", "harga emas"):
        prices, notes = await get_gold_prices_idr_per_gram()
        reply = format_price_message(prices, notes)
    elif cmd in ("help", "menu", "?", "hai", "halo", "hi"):
        reply = (
            "Menu:\n"
            "• *emas* / *gold* → harga emas IDR/gram (multi-source)\n"
            "• *help* → menu\n"
            f"⏱ {now_wib_str()}"
        )
    else:
        reply = "Ketik *emas* untuk cek harga emas (IDR/gram)."

    try:
        await wa_send_text(from_number, reply)
    except Exception as e:
        print("Reply send error:", repr(e))

    return JSONResponse({"ok": True})



