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
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()

# Optional (for spot price)
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
    """Lowercase and normalize whitespace."""
    return " ".join((s or "").lower().split())


def rupiah(n: int) -> str:
    # Format 1245000 -> "Rp 1.245.000"
    return "Rp " + f"{n:,}".replace(",", ".")


# =========================
# SOURCE FETCHERS
# =========================
async def fetch_antam_logammulia(client: httpx.AsyncClient) -> Optional[int]:
    """
    Best-effort scrape from LogamMulia.
    Site HTML changes occasionally; this uses a robust text regex.
    """
    url = "https://www.logammulia.com/id/harga-emas-hari-ini"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # Try to find "1 gr ... Rp ..."
        m = re.search(r"\b1\s*gr\b.{0,120}?\bRp\s*[\d\.\,]+", text, flags=re.IGNORECASE)
        if not m:
            return None
        m2 = re.search(r"Rp\s*[\d\.\,]+", m.group(0))
        if not m2:
            return None
        return clean_int_from_text(m2.group(0))
    except Exception as e:
        print("fetch_antam_logammulia error:", repr(e))
        return None


async def fetch_harga_emas_org(client: httpx.AsyncClient) -> Optional[int]:
    """
    Best-effort scrape from harga-emas.org for Emas 24K price.
    """
    url = "https://harga-emas.org/"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        m = re.search(r"Emas\s*24\s*Karat.{0,200}?Rp\s*[\d\.\,]+", text, flags=re.IGNORECASE)
        if not m:
            return None
        m2 = re.search(r"Rp\s*[\d\.\,]+", m.group(0))
        if not m2:
            return None
        return clean_int_from_text(m2.group(0))
    except Exception as e:
        print("fetch_harga_emas_org error:", repr(e))
        return None


async def fetch_spot_xau_usd_per_oz(client: httpx.AsyncClient) -> Optional[float]:
    """
    Spot XAU/USD price in USD per troy ounce using GoldAPI (optional).
    """
    if not GOLDAPI_KEY:
        return None
    url = "https://www.goldapi.io/api/XAU/USD"
    headers = {"x-access-token": GOLDAPI_KEY}
    try:
        r = await client.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        price = data.get("price")
        if price is None:
            return None
        return float(price)
    except Exception as e:
        print("fetch_spot_xau_usd_per_oz error:", repr(e))
        return None


async def fetch_usd_idr_rate(client: httpx.AsyncClient) -> Optional[float]:
    """
    USD->IDR exchange rate using exchangerate-api.com (optional).
    Replace this provider if you prefer.
    """
    if not EXCHANGERATE_API_KEY:
        return None

    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGERATE_API_KEY}/latest/USD"
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        rates = data.get("conversion_rates") or {}
        idr = rates.get("IDR")
        if not idr:
            return None
        return float(idr)
    except Exception as e:
        print("fetch_usd_idr_rate error:", repr(e))
        return None


def xau_usd_oz_to_idr_per_gram(xau_usd_per_oz: float, usd_idr: float) -> int:
    # 1 troy ounce = 31.1034768 grams
    return int(round((xau_usd_per_oz * usd_idr) / 31.1034768))


# =========================
# AGGREGATION
# =========================
async def get_gold_prices_idr_per_gram() -> Tuple[Dict[str, int], List[str]]:
    cached = cache_get("gold_prices")
    if cached:
        return cached

    prices: Dict[str, int] = {}
    notes: List[str] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        antam = await fetch_antam_logammulia(client)
        if antam:
            prices["Antam (LogamMulia)"] = antam
        else:
            notes.append("Antam unavailable")

        he = await fetch_harga_emas_org(client)
        if he:
            prices["Harga-Emas.org (24K)"] = he
        else:
            notes.append("Harga-Emas unavailable")

        spot = await fetch_spot_xau_usd_per_oz(client)
        fx = await fetch_usd_idr_rate(client)
        if spot and fx:
            prices["Spot (XAU/USD→IDR)"] = xau_usd_oz_to_idr_per_gram(spot, fx)
        else:
            # only note if user expects it
            if GOLDAPI_KEY or EXCHANGERATE_API_KEY:
                notes.append("Spot unavailable (API issue)")
            else:
                notes.append("Spot disabled (no API keys)")

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

    lines = ["💰 Harga Emas (IDR/gram)", "──────────────"]
    for k, v in prices.items():
        lines.append(f"{k}: {rupiah(v)}")

    lines.append("──────────────")
    lines.append(f"📊 Median: {rupiah(median)}")
    if len(vals) >= 2:
        lines.append(f"↔️ Spread: {rupiah(spread)}")

    # keep notes short
    if notes:
        lines.append("")
        lines.append("ℹ️ " + " | ".join(notes[:2]))

    lines.append(f"⏱ {now_wib_str()}")
    return "\n".join(lines)


# =========================
# WHATSAPP SEND
# =========================
async def wa_send_text(to: str, body: str) -> None:
    """
    Send a text reply via WhatsApp Cloud API.
    """
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
        r = await client.post(url, headers=headers, json=payload, timeout=15)
        print("SEND STATUS:", r.status_code)
        print("SEND BODY:", r.text[:500])  # truncate
        # Raise if not ok (so logs show error if any)
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
    Meta webhook verification.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    # Verification request from Meta
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge)

    # Non-verification GETs (optional)
    return PlainTextResponse("OK", status_code=200)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive inbound messages and reply.
    """
    data = await request.json()
    print("WEBHOOK:", json.dumps(data)[:1000])  # truncate for logs

    # Meta payload: entry -> changes -> value
    entry = (data.get("entry") or [])
    if not entry:
        return JSONResponse({"ok": True})

    changes = (entry[0].get("changes") or [])
    if not changes:
        return JSONResponse({"ok": True})

    value = (changes[0].get("value") or {})

    # Ignore status updates (delivery receipts) — they have no messages
    messages = value.get("messages") or []
    if not messages:
        # likely statuses event
        return JSONResponse({"ok": True})

    msg = messages[0]
    from_number = msg.get("from")  # digits only, e.g. "62812..."
    msg_type = msg.get("type")

    text_body = ""
    if msg_type == "text":
        text_body = ((msg.get("text") or {}).get("body") or "")
    cmd = normalize_cmd(text_body)

    print("FROM:", from_number, "TYPE:", msg_type, "TEXT:", text_body)

    if not from_number:
        return JSONResponse({"ok": True})

    # Commands
    if cmd in ("emas", "gold", "harga emas", "price", "antam"):
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
