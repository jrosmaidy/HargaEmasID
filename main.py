import os
import re
import time
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Tuple, List

import pytz
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse

# =========================
# Config
# =========================
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")

GOLDAPI_KEY = os.getenv("GOLDAPI_KEY", "")
EXCHANGERATE_API_KEY = os.getenv("EXCHANGERATE_API_KEY", "")

if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID or not VERIFY_TOKEN:
    # Don't crash import in some platforms, but do fail loudly on usage.
    pass

TZ_JAKARTA = pytz.timezone("Asia/Jakarta")

# cache (very simple in-memory, good enough for a single instance)
CACHE_TTL_SECONDS = 300  # 5 minutes
_cache: Dict[str, Tuple[float, object]] = {}

app = FastAPI()


# =========================
# Utilities
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
    """
    Extract integer from a price string like 'Rp 1.245.000' or '1,245,000'.
    Returns None if cannot parse.
    """
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


@dataclass
class PriceQuote:
    name: str
    idr_per_gram: int
    detail: str = ""


# =========================
# Source fetchers
# =========================
async def fetch_antam_logammulia(client: httpx.AsyncClient) -> Optional[PriceQuote]:
    """
    Attempts to fetch Antam price from logammulia.com (HTML changes occasionally).
    This parser is best-effort and may need selector updates.
    """
    url = "https://www.logammulia.com/id/harga-emas-hari-ini"
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # Best-effort: look for "Emas Batangan" + "1 gr" row and parse its price.
        text = soup.get_text(" ", strip=True)

        # Heuristic regex: find something like "1 gr ... Rp 1.245.000"
        m = re.search(r"\b1\s*gr\b.{0,80}?\bRp\s*[\d\.\,]+", text, flags=re.IGNORECASE)
        if not m:
            return None
        price_text = m.group(0)
        # pull last Rp price
        m2 = re.search(r"Rp\s*[\d\.\,]+", price_text)
        if not m2:
            return None
        parsed = clean_int_from_text(m2.group(0))
        if not parsed:
            return None
        return PriceQuote(name="Antam (LogamMulia)", idr_per_gram=parsed, detail="1gr")
    except Exception:
        return None


async def fetch_harga_emas_org(client: httpx.AsyncClient) -> Optional[PriceQuote]:
    """
    Best-effort scraping for harga-emas.org or similar pages.
    Site structures vary—update selector/regex if it breaks.
    """
    url = "https://harga-emas.org/"
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        # Heuristic: locate 'Emas 24 Karat' then an 'Rp' price close by.
        m = re.search(r"Emas\s*24\s*Karat.{0,120}?Rp\s*[\d\.\,]+", text, flags=re.IGNORECASE)
        if not m:
            return None
        m2 = re.search(r"Rp\s*[\d\.\,]+", m.group(0))
        if not m2:
            return None
        parsed = clean_int_from_text(m2.group(0))
        if not parsed:
            return None
        return PriceQuote(name="Harga-Emas.org", idr_per_gram=parsed, detail="24K")
    except Exception:
        return None


async def fetch_spot_xau_usd(client: httpx.AsyncClient) -> Optional[float]:
    """
    Spot XAU price in USD per ounce (troy ounce).
    Uses GoldAPI if key exists, otherwise None.
    """
    if not GOLDAPI_KEY:
        return None

    # GoldAPI docs: https://www.goldapi.io/
    url = "https://www.goldapi.io/api/XAU/USD"
    headers = {"x-access-token": GOLDAPI_KEY}
    try:
        r = await client.get(url, headers=headers, timeout=12)
        r.raise_for_status()
        data = r.json()
        # commonly: {"price": 2xxx.xx, ...} USD per ounce
        price = data.get("price")
        if price is None:
            return None
        return float(price)
    except Exception:
        return None


async def fetch_usd_idr_rate(client: httpx.AsyncClient) -> Optional[float]:
    """
    USD -> IDR rate. Replace provider if you prefer.
    Example uses exchangerate-api.com if key exists.
    """
    if not EXCHANGERATE_API_KEY:
        return None

    # Example endpoint for exchangerate-api.com (adjust to your plan/provider):
    url = f"https://v6.exchangerate-api.com/v6/{EXCHANGERATE_API_KEY}/latest/USD"
    try:
        r = await client.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        rates = data.get("conversion_rates") or {}
        idr = rates.get("IDR")
        if not idr:
            return None
        return float(idr)
    except Exception:
        return None


def xau_usd_oz_to_idr_per_gram(xau_usd_per_oz: float, usd_idr: float) -> int:
    """
    1 troy ounce = 31.1034768 grams
    """
    idr_per_oz = xau_usd_per_oz * usd_idr
    idr_per_gram = idr_per_oz / 31.1034768
    return int(round(idr_per_gram))


# =========================
# Aggregation
# =========================
async def get_gold_prices_idr_per_gram() -> Tuple[Dict[str, int], List[str]]:
    """
    Returns: (prices_dict, notes)
    prices_dict values are IDR per gram.
    """
    cached = cache_get("gold_prices")
    if cached:
        return cached

    prices: Dict[str, int] = {}
    notes: List[str] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        # Scraped local sources
        antam = await fetch_antam_logammulia(client)
        if antam:
            prices[antam.name] = antam.idr_per_gram
        else:
            notes.append("Antam source unavailable")

        he = await fetch_harga_emas_org(client)
        if he:
            prices[he.name] = he.idr_per_gram
        else:
            notes.append("Harga-Emas.org source unavailable")

        # Spot fallback (needs keys)
        spot_usd = await fetch_spot_xau_usd(client)
        fx = await fetch_usd_idr_rate(client)
        if spot_usd and fx:
            prices["Spot (XAU/USD→IDR)"] = xau_usd_oz_to_idr_per_gram(spot_usd, fx)
        else:
            notes.append("Spot source unavailable (missing/failed API keys)")

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
        lines.append(f"{k}: Rp {v:,}".replace(",", "."))

    lines.append("──────────────")
    lines.append(f"📊 Median: Rp {median:,}".replace(",", "."))
    if len(vals) >= 2:
        lines.append(f"↔️ Spread: Rp {spread:,}".replace(",", "."))

    if notes:
        # keep it short
        lines.append("")
        lines.append("ℹ️ " + " | ".join(notes[:2]))

    lines.append(f"⏱ {now_wib_str()}")
    return "\n".join(lines)


# =========================
# WhatsApp Cloud API send
# =========================
async def wa_send_text(to: str, body: str) -> None:
    url = f"https://graph.facebook.com/v22.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, headers=headers, json=payload, timeout=12)
        # If this fails, log details for debugging:
        if r.status_code >= 300:
            raise HTTPException(status_code=500, detail=f"WA send failed: {r.status_code} {r.text}")


# =========================
# Webhook endpoints
# =========================
@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta webhook verification.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN and challenge:
        return PlainTextResponse(challenge)
    return PlainTextResponse("Verification failed", status_code=403)


@app.post("/webhook")
async def receive_webhook(request: Request):
    """
    Receive inbound messages and reply with gold price.
    """
    data = await request.json()

    # Meta sends nested structure. We only handle message events.
    # Structure example:
    # entry[0].changes[0].value.messages[0].text.body
    try:
        entry = data.get("entry", [])
        if not entry:
            return JSONResponse({"ok": True})

        changes = entry[0].get("changes", [])
        if not changes:
            return JSONResponse({"ok": True})

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return JSONResponse({"ok": True})

        msg = messages[0]
        from_number = msg.get("from")  # sender WA ID (digits)
        msg_type = msg.get("type")

        text_body = ""
        if msg_type == "text":
            text_body = (msg.get("text") or {}).get("body", "")
        else:
            text_body = ""

        cmd = text_body.strip().lower()

        # Commands
        if cmd in ("emas", "gold", "harga emas", "price", "antam"):
            prices, notes = await get_gold_prices_idr_per_gram()
            reply = format_price_message(prices, notes)
        elif cmd in ("help", "menu", "?", "hai", "halo"):
            reply = (
                "Ketik:\n"
                "• *emas* / *gold* → harga emas IDR/gram (multi-source)\n"
                "• *help* → menu\n"
                f"⏱ {now_wib_str()}"
            )
        else:
            reply = "Ketik *emas* untuk cek harga emas IDR/gram."

        if from_number:
            await wa_send_text(from_number, reply)

        return JSONResponse({"ok": True})
    except Exception as e:
        # Return 200 to avoid retries storm; log exception in your platform logs.
        return JSONResponse({"ok": True, "error": str(e)})
