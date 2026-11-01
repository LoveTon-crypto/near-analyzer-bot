"""NEAR Analyzer Bot.

• Alerts into topics of a Telegram forum group:
    dexpaid — a NEAR token paid for a DEX Screener profile (+ follow-up when approved)
    boost / cto / ads — DEX Screener boosts, community takeovers, ads
    10k / 20k / 30k — nearly / JustHoot token reached that market cap
    dip — memepad token dropped 15%+ within an hour
• Token scanner (same as in near-dex-bot): send a token address or link → price, FDV/ATH,
  liquidity, volume, top holders and a 15m candle chart.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import time

import httpx
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions,
                      ReplyParameters, Update)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          filters)

import calls
import xwatch
import digest
import nearprice
import tops
import deploys
import wallet as walletmod
import pnl_art
from scanner import Scanner, TokenCard, age_fmt, extract_address, pct_fmt, price_fmt, render_chart, usd

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("near-analyzer")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyzer.db"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))
PENDING_RECHECK_SECONDS = int(os.environ.get("PENDING_RECHECK_SECONDS", "120"))
PENDING_MAX_HOURS = float(os.environ.get("PENDING_MAX_HOURS", "12"))
CHAIN = "near"
DS = "https://api.dexscreener.com"
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

TOPICS = {
    "dexpaid": "DEX Paid (paid DEX Screener profile)",
    "boost": "DEX Screener boosts",
    "cto": "Community Takeover (CTO)",
    "ads": "DEX Screener ads",
}
# Memepad market-cap milestones (no bonding curve on nearly / JustHoot): MCAP_LEVELS=10000,20000,30000
MCAP_LEVELS = sorted(int(float(x)) for x in os.environ.get("MCAP_LEVELS", "10000,20000,30000").split(",") if x.strip())
MEMEPAD_POLL_SECONDS = int(os.environ.get("MEMEPAD_POLL_SECONDS", "60"))
DISCOVER_SECONDS = int(os.environ.get("DISCOVER_SECONDS", "180"))
DIP_PCT = float(os.environ.get("DIP_PCT", "15"))
DIP_WINDOW_MIN = int(os.environ.get("DIP_WINDOW_MIN", "60"))
DIP_COOLDOWN_MIN = int(os.environ.get("DIP_COOLDOWN_MIN", "60"))
DIP_MIN_LIQ = float(os.environ.get("DIP_MIN_LIQ", "300"))


def level_key(v: int) -> str:
    return f"{v // 1000}k" if v % 1000 == 0 else str(v)


for _lvl in MCAP_LEVELS:
    TOPICS[level_key(_lvl)] = f"Memepad token hit ${level_key(_lvl).upper()} market cap"
TOPICS["dip"] = f"Memepad dip alert (-{DIP_PCT:g}% or more)"
TOPICS["x"] = "Posts from watched X accounts (/xadd)"
TOPICS["daily"] = "Daily top-5 gainers / top-3 losers (24h)"
DIGEST_TIME = os.environ.get("DIGEST_TIME", "18:00")          # local time of the daily post
DIGEST_TZ = os.environ.get("DIGEST_TZ", "Europe/Minsk")

PADS = {  # suffix -> (name, link template)
    ".nearlytrade.near": ("Nearly", "https://nearly.trade/{a}"),
    ".justhoot.near": ("JustHoot", "https://justhoot.fun/p/{a}"),
}
# DEX Screener search queries that surface memepad tokens (same as near-dex-bot)
PAD_QUERIES = ["nearlytrade", "nearlytrade.near", "nearlytrade wNEAR", "nearlytrade NEARLY", "nearlytrade RHEA",
               "justhoot", "hoot.near", "launchpad.justhoot.near", "v2-launchpad.justhoot.near", "justhoot wNEAR"]

ORDER_NAMES = {"tokenProfile": "Paid token profile", "communityTakeover": "Community Takeover",
               "tokenAd": "Token ad", "trendingBarAd": "Trending bar ad"}
STATUS_ICONS = {"approved": "✅", "processing": "⏳", "on-hold": "⏸", "rejected": "❌", "cancelled": "🚫"}


def pad_of(addr: str):
    for suffix, info in PADS.items():
        if addr.endswith(suffix):
            return info
    return None


# ---------- storage ----------
class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(pending)").fetchall()]
        if cols and "thread_id" not in cols:  # table from the first (no-dependency) version
            self.db.execute("DROP TABLE pending")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS topics (key TEXT PRIMARY KEY, chat_id INTEGER, thread_id INTEGER);
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS pending (token TEXT, order_type TEXT, status TEXT, chat_id INTEGER,
                                                message_id INTEGER, thread_id INTEGER, created INTEGER,
                                                PRIMARY KEY (token, order_type));
            CREATE TABLE IF NOT EXISTS calls (chat_id INTEGER, token TEXT, user TEXT, mcap REAL, ts REAL,
                                              PRIMARY KEY (chat_id, token));
            CREATE TABLE IF NOT EXISTS scan_keys (key TEXT PRIMARY KEY, token TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS pad_tokens (address TEXT PRIMARY KEY, first_seen REAL);
        """)
        self.db.commit()
        calls.migrate(self.db)
        xwatch.setup(self.db)

    def q(self, sql, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur.fetchall()

    def set_topic(self, key, chat_id, thread_id):
        self.q("REPLACE INTO topics VALUES (?,?,?)", (key, chat_id, thread_id))

    def topic(self, key):
        r = self.q("SELECT chat_id, thread_id FROM topics WHERE key=?", (key,))
        return r[0] if r else None

    def seen(self, key) -> bool:
        return bool(self.q("SELECT 1 FROM seen WHERE key=?", (key,)))

    def mark(self, key):
        self.q("INSERT OR IGNORE INTO seen VALUES (?,?)", (key, int(time.time())))

    def kv_get(self, k):
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        return r[0][0] if r else None

    def kv_set(self, k, v):
        self.q("REPLACE INTO kv VALUES (?,?)", (k, v))

    def scan_key(self, token: str) -> str:
        key = hashlib.sha1(token.encode()).hexdigest()[:16]
        self.q("INSERT OR IGNORE INTO scan_keys VALUES (?,?)", (key, token))
        return key

    def token_by_key(self, key: str):
        r = self.q("SELECT token FROM scan_keys WHERE key=?", (key,))
        return r[0][0] if r else None


store = Store(DB_PATH)
scanner = Scanner()
store.db.executescript("""
    CREATE TABLE IF NOT EXISTS onchain_px (token TEXT, ts REAL, price REAL);
    CREATE INDEX IF NOT EXISTS onchain_px_t ON onchain_px(token, ts);
""")
_px_last: dict[str, float] = {}


def _px_sink(token: str, ts: float, price: float):
    """Price from the Ref DCL pool → history for the chart (at most one point per 30s per token)."""
    if price <= 0 or ts - _px_last.get(token, 0) < 30:
        return
    _px_last[token] = ts
    store.db.execute("INSERT INTO onchain_px VALUES (?,?,?)", (token, ts, price))
    if len(_px_last) and int(ts) % 50 == 0:
        store.db.execute("DELETE FROM onchain_px WHERE ts<?", (ts - 7 * 86400,))
    store.db.commit()


scanner.px_sink = _px_sink
scanner.px_hist = lambda token, since: store.q("SELECT ts, price FROM onchain_px WHERE token=? AND ts>=?",
                                              (token, since))
http = httpx.AsyncClient(timeout=20, headers={"User-Agent": "near-analyzer-bot/1.0"})
feed = deploys.DeployFeed(store, scanner, http)
wallets = walletmod.WalletPnL(http)


async def ds_get(path: str):
    try:
        r = await http.get(DS + path)
        if r.status_code == 429:
            log.warning("DEX Screener rate limit, backing off")
            await asyncio.sleep(30)
            return None
        if r.status_code >= 400:
            log.info("DEX Screener %s -> HTTP %s", path, r.status_code)
            return None
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.info("DEX Screener %s failed: %s", path, e)
        return None


async def get_orders(token: str) -> list:
    data = await ds_get(f"/orders/v1/{CHAIN}/{token}")
    if isinstance(data, dict):  # newer shape {"orders": [...], "boosts": [...]}
        data = data.get("orders") or []
    return data if isinstance(data, list) else []


async def best_pair(token: str):
    data = await ds_get(f"/tokens/v1/{CHAIN}/{token}")
    if isinstance(data, dict):
        data = data.get("pairs") or []
    if not isinstance(data, list) or not data:
        return None
    mine = [p for p in data if (p.get("baseToken") or {}).get("address", "").lower() == token.lower()] or data
    return max(mine, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)


# ---------- alert formatting ----------
def links_line(item: dict, pair: dict | None) -> str:
    out, used = [], set()
    links = list(item.get("links") or [])
    info = (pair or {}).get("info") or {}
    links += [{"label": w.get("label") or "Website", "url": w.get("url")} for w in info.get("websites") or []]
    links += [{"type": s.get("type"), "url": s.get("url")} for s in info.get("socials") or []]
    for link in links:
        url = link.get("url")
        if not url or url in used:
            continue
        used.add(url)
        kind = (link.get("type") or "").lower()
        label = {"twitter": "X", "telegram": "Telegram", "discord": "Discord"}.get(kind, link.get("label") or "Website")
        out.append(f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>')
    return " · ".join(out)


def token_card(token: str, item: dict, pair: dict | None) -> list[str]:
    base = (pair or {}).get("baseToken") or {}
    sym = base.get("symbol") or token.split(".")[0].split("-")[0].upper()
    name = base.get("name") or ""
    lines = [f"<b>${html.escape(sym)}</b>" + (f" — {html.escape(name)}" if name else "")]
    if pair:
        mc = pair.get("marketCap") or pair.get("fdv")
        liq = (pair.get("liquidity") or {}).get("usd")
        ch = (pair.get("priceChange") or {}).get("h24")
        stats = f"💰 MC: {usd(mc)} · 💧 Liq: {usd(liq)} · ⏱ {age_fmt(pair.get('pairCreatedAt') or 0)}"
        if ch is not None:
            stats += f" · 24h: {float(ch):+.1f}%"
        lines.append(stats)
    pad = pad_of(token)
    if pad:
        lines.append(f"🚀 Launchpad: {pad[0]}")
    lines.append(f"📄 CA: <code>{html.escape(token)}</code>")
    ll = links_line(item, pair)
    if ll:
        lines.append(f"🔗 {ll}")
    return lines


def alert_keyboard(token: str, item: dict, pair: dict | None) -> InlineKeyboardMarkup:
    ds_url = item.get("url") or (pair or {}).get("url") or f"https://dexscreener.com/{CHAIN}/{token}"
    row = [InlineKeyboardButton("🔍 NearBlocks" if (pair or {}).get("_onchain") else "📈 DexScreener", url=ds_url)]
    pad = pad_of(token)
    if pad:
        row.append(InlineKeyboardButton(pad[0], url=pad[1].format(a=token)))
    buy = [InlineKeyboardButton("🟢 Buy on Bettear", url=deploys.buy_link(token))]
    return InlineKeyboardMarkup([buy, row, [InlineKeyboardButton("🔎 Scan", callback_data="sc:" + store.scan_key(token))]])


def image_of(item: dict, pair: dict | None):
    info = (pair or {}).get("info") or {}
    return item.get("header") or info.get("header") or item.get("icon") or info.get("imageUrl")


async def post(ctx, topic_key: str, text: str, image=None, kb=None, reply_to: int | None = None):
    t = store.topic(topic_key)
    if not t:
        log.info("No topic bound for %s, skipping", topic_key)
        return None
    chat_id, thread_id = t
    kw = dict(chat_id=chat_id, parse_mode=ParseMode.HTML, reply_markup=kb)
    if thread_id:
        kw["message_thread_id"] = thread_id
    if reply_to:
        kw["reply_parameters"] = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
    if image and len(re.sub(r"<[^>]+>", "", text)) <= 1024:
        try:
            return await ctx.bot.send_photo(photo=image, caption=text, **kw)
        except Exception as e:  # bad image url etc. — fall back to text
            log.info("send_photo failed (%s), sending text", e)
    try:
        return await ctx.bot.send_message(text=text, link_preview_options=NO_PREVIEW, **kw)
    except Exception as e:
        log.warning("Can't post to %s: %s", topic_key, e)
        return None


# ---------- DEX Screener watchers ----------
async def handle_profile(ctx, item: dict, silent: bool):
    token = item.get("tokenAddress")
    key = f"profile:{token}"
    if not token or store.seen(key):
        return
    store.mark(key)
    if silent:
        return
    orders = await get_orders(token)
    prof = [o for o in orders if o.get("type") == "tokenProfile"] or [{"type": "tokenProfile", "status": "approved"}]
    status = prof[0].get("status") or "approved"
    pair = await best_pair(token)
    lines = [f"💎 <b>DEX PAID</b> {STATUS_ICONS.get(status, '•')} {status.upper()}", ""] + token_card(token, item, pair)
    if prof[0].get("paymentTimestamp"):
        lines.append(f"🕒 Paid {age_fmt(prof[0]['paymentTimestamp'])} ago")
    other = [o for o in orders if o.get("type") != "tokenProfile" and o.get("status") == "approved"]
    if other:
        lines.append("➕ Also paid: " + ", ".join(ORDER_NAMES.get(o["type"], o["type"]) for o in other))
    msg = await post(ctx, "dexpaid", "\n".join(lines), image_of(item, pair), alert_keyboard(token, item, pair))
    log.info("DEX paid alert %s (%s)", token, status)
    if msg and status in ("processing", "on-hold"):
        store.q("REPLACE INTO pending VALUES (?,?,?,?,?,?,?)",
                (token, "tokenProfile", status, msg.chat_id, msg.message_id, msg.message_thread_id or 0,
                 int(time.time())))


async def handle_boost(ctx, item: dict, silent: bool):
    token, total = item.get("tokenAddress"), item.get("totalAmount") or item.get("amount")
    key = f"boost:{token}:{total}"
    if not token or store.seen(key):
        return
    store.mark(key)
    if silent:
        return
    pair = await best_pair(token)
    lines = [f"⚡ <b>BOOST</b> +{item.get('amount', '?')} (total {total})", ""] + token_card(token, item, pair)
    await post(ctx, "boost", "\n".join(lines), image_of(item, pair), alert_keyboard(token, item, pair))


async def handle_cto(ctx, item: dict, silent: bool):
    token = item.get("tokenAddress")
    key = f"cto:{token}"
    if not token or store.seen(key):
        return
    store.mark(key)
    if silent:
        return
    pair = await best_pair(token)
    lines = ["🏴 <b>COMMUNITY TAKEOVER</b> approved", ""] + token_card(token, item, pair)
    await post(ctx, "cto", "\n".join(lines), image_of(item, pair), alert_keyboard(token, item, pair))


async def handle_ad(ctx, item: dict, silent: bool):
    token = item.get("tokenAddress")
    key = f"ad:{token}:{item.get('date')}:{item.get('type')}"
    if not token or store.seen(key):
        return
    store.mark(key)
    if silent:
        return
    pair = await best_pair(token)
    dur = item.get("durationHours")
    head = f"📢 <b>DEX SCREENER AD</b> ({item.get('type', 'ad')}" + (f", {dur}h" if dur else "") + ")"
    lines = [head, ""] + token_card(token, item, pair)
    await post(ctx, "ads", "\n".join(lines), image_of(item, pair), alert_keyboard(token, item, pair))


FEEDS = (("/token-profiles/latest/v1", handle_profile), ("/token-boosts/latest/v1", handle_boost),
         ("/community-takeovers/latest/v1", handle_cto), ("/ads/latest/v1", handle_ad))


async def poll_feeds(ctx, silent: bool = False):
    for path, handler in FEEDS:
        data = await ds_get(path)
        if isinstance(data, dict):
            data = [data]
        for item in data if isinstance(data, list) else []:
            if (item.get("chainId") or "").lower() != CHAIN:
                continue
            try:
                await handler(ctx, item, silent)
            except Exception:
                log.exception("%s failed", handler.__name__)


async def recheck_pending(ctx):
    now = time.time()
    for token, otype, old, chat_id, msg_id, thread_id, created in store.q("SELECT * FROM pending"):
        if now - created > PENDING_MAX_HOURS * 3600:
            store.q("DELETE FROM pending WHERE token=? AND order_type=?", (token, otype))
            continue
        orders = [o for o in await get_orders(token) if o.get("type") == otype]
        new = orders[0].get("status") if orders else None
        if not new or new == old:
            continue
        sym = token.split(".")[0].split("-")[0].upper()
        await post(ctx, "dexpaid",
                   f"{STATUS_ICONS.get(new, '•')} <b>{ORDER_NAMES.get(otype, otype)} {new}</b> by DEX Screener\n"
                   f"<code>{html.escape(token)}</code> (${html.escape(sym)})", reply_to=msg_id)
        if new in ("processing", "on-hold"):
            store.q("UPDATE pending SET status=? WHERE token=? AND order_type=?", (new, token, otype))
        else:
            store.q("DELETE FROM pending WHERE token=? AND order_type=?", (token, otype))


async def watcher(ctx: ContextTypes.DEFAULT_TYPE):
    if not store.kv_get("seeded"):
        log.info("First start: seeding current feeds without alerts")
        await poll_feeds(ctx, silent=True)
        store.kv_set("seeded", "1")
        return
    await poll_feeds(ctx)
    last = float(store.kv_get("last_recheck") or 0)
    if time.time() - last > PENDING_RECHECK_SECONDS:
        store.kv_set("last_recheck", str(time.time()))
        await recheck_pending(ctx)


# ---------- memepads: market-cap milestones and dips ----------
price_hist: dict[str, list[tuple[float, float]]] = {}   # token -> [(ts, price)] for the dip window
last_dip: dict[str, float] = {}


def pair_mcap(p: dict) -> float:
    try:
        return float(p.get("marketCap") or p.get("fdv") or 0)
    except (TypeError, ValueError):
        return 0.0


def is_pad_pair(p: dict) -> bool:
    return p.get("chainId") == CHAIN and pad_of(((p.get("baseToken") or {}).get("address") or "")) is not None


async def discover_pad_tokens(ctx):
    """Find memepad tokens via DEX Screener search; new ones get their already-passed levels marked silently."""
    found: dict[str, dict] = {}
    for q in PAD_QUERIES:
        data = await ds_get("/latest/dex/search?q=" + q.replace(" ", "%20")) or {}
        for p in data.get("pairs") or []:
            if is_pad_pair(p):
                addr = p["baseToken"]["address"]
                if addr not in found or ((p.get("liquidity") or {}).get("usd") or 0) > \
                        ((found[addr].get("liquidity") or {}).get("usd") or 0):
                    found[addr] = p
    first_run = not store.kv_get("pads_seeded")
    for addr, p in found.items():
        if store.q("SELECT 1 FROM pad_tokens WHERE address=?", (addr,)):
            continue
        store.q("INSERT INTO pad_tokens VALUES (?,?)", (addr, time.time()))
        age_h = (time.time() - (p.get("pairCreatedAt") or 0) / 1000) / 3600
        if first_run or age_h > 2:  # old token: don't announce levels it passed before we saw it
            for lvl in MCAP_LEVELS:
                if pair_mcap(p) >= lvl:
                    store.mark(f"mcap:{addr}:{lvl}")
    if first_run:
        store.kv_set("pads_seeded", "1")
        log.info("Memepads: seeded %d tokens", len(found))


ONCHAIN_DAYS = int(os.environ.get("ONCHAIN_DAYS", "7"))
# Memepad tokens always watched and listed in /top, even if DexScreener search never surfaced them
PAD_EXTRA = [x.strip() for x in os.environ.get("PAD_EXTRA", "justai.nearlytrade.near").split(",") if x.strip()]
ONCHAIN_PROBES = int(os.environ.get("ONCHAIN_PROBES", "3"))   # new pool lookups per minute


def add_deployed_pads():
    """Memepad tokens seen by the deploys feed join the MC/dip watch right away, not only after DexScreener finds them."""
    since = time.time() - ONCHAIN_DAYS * 86400
    rows = store.q("SELECT token, ts FROM deploys WHERE ts>=?", (since,)) + \
        store.q("SELECT token, MIN(ts) FROM calls WHERE ts>=? GROUP BY token", (since,))
    for (tok, ts) in rows:
        if pad_of(tok) is None or store.q("SELECT 1 FROM pad_tokens WHERE address=?", (tok,)):
            continue
        store.q("INSERT INTO pad_tokens VALUES (?,?)", (tok, ts or time.time()))
        if time.time() - (ts or 0) > 86400:  # deployed >24h ago: don't announce levels it passed long before
            store.mark(f"padseed:{tok}")


def ensure_pad_token(addr: str) -> bool:
    """Add a memepad token to the watch list (/top, MC levels, dips). Returns True if it was new.
    Levels it already passed are marked silently on the next memepad check (no late alerts)."""
    if not addr or pad_of(addr) is None or store.q("SELECT 1 FROM pad_tokens WHERE address=?", (addr,)):
        return False
    store.q("INSERT INTO pad_tokens VALUES (?,?)", (addr, time.time()))
    store.mark(f"padseed:{addr}")
    log.info("Memepad token added: %s", addr)
    return True


def add_extra_pads():
    for addr in PAD_EXTRA:
        ensure_pad_token(addr)


def best_pairs(data) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in data if isinstance(data, list) else []:
        if is_pad_pair(p):
            addr = p["baseToken"]["address"]
            if addr not in out or ((p.get("liquidity") or {}).get("usd") or 0) > \
                    ((out[addr].get("liquidity") or {}).get("usd") or 0):
                out[addr] = p
    return out


async def check_memepads(ctx: ContextTypes.DEFAULT_TYPE):
    last = float(store.kv_get("last_discover") or 0)
    if time.time() - last > DISCOVER_SECONDS:
        store.kv_set("last_discover", str(time.time()))
        await discover_pad_tokens(ctx)
    add_deployed_pads()
    add_extra_pads()
    tokens = [r[0] for r in store.q("SELECT address FROM pad_tokens")]
    fresh = {r[0] for r in store.q("SELECT address FROM pad_tokens WHERE first_seen>=?",
                                   (time.time() - ONCHAIN_DAYS * 86400,))}
    now = time.time()
    budget = {"probe": ONCHAIN_PROBES}
    for i in range(0, len(tokens), 30):
        chunk = tokens[i:i + 30]
        pairs = best_pairs(await ds_get(f"/tokens/v1/{CHAIN}/" + ",".join(chunk)))
        for addr in chunk:  # migrated but DexScreener hasn't indexed the pool yet → price from Ref DCL on-chain
            if addr not in pairs and addr in fresh:
                known = scanner.has_dcl_pool(addr)
                if known is False:
                    continue  # no pool (still on the memepad) — re-checked hourly, not every minute
                if known is None:
                    if budget["probe"] <= 0:
                        continue  # pool lookups cost several RPC calls: a few tokens per minute only
                    budget["probe"] -= 1
                try:
                    op = await scanner.onchain_pair(addr)
                except Exception:
                    log.exception("onchain pair %s", addr)
                    op = None
                if op:
                    pairs[addr] = op
        for addr, p in pairs.items():
            if store.seen(f"padseed:{addr}"):  # old token we just learned about: mark passed levels silently
                for lvl in MCAP_LEVELS:
                    if pair_mcap(p) >= lvl:
                        store.mark(f"mcap:{addr}:{lvl}")
                store.q("DELETE FROM seen WHERE key=?", (f"padseed:{addr}",))
            try:
                await check_levels(ctx, addr, p)
                await check_dip(ctx, addr, p, now)
            except Exception:
                log.exception("memepad check %s failed", addr)


async def check_levels(ctx, addr: str, p: dict):
    mc = pair_mcap(p)
    hit = [lvl for lvl in MCAP_LEVELS if mc >= lvl and not store.seen(f"mcap:{addr}:{lvl}")]
    for lvl in hit:
        store.mark(f"mcap:{addr}:{lvl}")
    if not hit:
        return
    lvl = max(hit)  # jumped over several levels at once → one alert in the highest topic
    item = {"url": p.get("url")}
    lines = [f"🎯 <b>${level_key(lvl).upper()} MARKET CAP</b>", ""] + token_card(addr, item, p)
    vol = (p.get("volume") or {}).get("h1")
    tx = ((p.get("txns") or {}).get("h1") or {})
    if vol is not None:
        lines.insert(4, f"🔄 1h vol: {usd(vol)} · 🟢 {tx.get('buys', 0)} / 🔴 {tx.get('sells', 0)}")
    await post(ctx, level_key(lvl), "\n".join(lines), image_of(item, p), alert_keyboard(addr, item, p))
    log.info("MCAP %s alert %s (%.0f)", level_key(lvl), addr, mc)


async def check_dip(ctx, addr: str, p: dict, now: float):
    try:
        price = float(p.get("priceUsd") or 0)
    except (TypeError, ValueError):
        return
    if price <= 0:
        return
    hist = [(t, v) for t, v in price_hist.get(addr, []) if now - t <= DIP_WINDOW_MIN * 60]
    hist.append((now, price))
    price_hist[addr] = hist
    if ((p.get("liquidity") or {}).get("usd") or 0) < DIP_MIN_LIQ:
        return
    peak = max(v for _, v in hist)
    drop = (1 - price / peak) * 100
    if drop < DIP_PCT or now - last_dip.get(addr, 0) < DIP_COOLDOWN_MIN * 60:
        return
    last_dip[addr] = now
    price_hist[addr] = [(now, price)]  # next dip is measured from here
    peak_ago = int((now - max(hist, key=lambda r: r[1])[0]) // 60)
    item = {"url": p.get("url")}
    lines = [f"🩸 <b>DIP −{drop:.1f}%</b> in {max(peak_ago, 1)}m", ""] + token_card(addr, item, p)
    await post(ctx, "dip", "\n".join(lines), image_of(item, p), alert_keyboard(addr, item, p))
    log.info("DIP alert %s -%.1f%%", addr, drop)


# ---------- token scanner (same card as near-dex-bot) ----------
def nb(acc: str) -> str:
    return f"https://nearblocks.io/address/{html.escape(acc)}"


def fmt_card(c: TokenCard, caller: "calls.Call | None") -> str:
    link = html.escape(c.pair_url) if c.pair_url else (
        f"https://nearly.trade/{html.escape(c.address)}" if c.src == "nearly" else nb(c.address))
    head = f"${html.escape(c.symbol.upper())} · ⏱ {age_fmt(c.created_ms)}"
    pad = pad_of(c.address)
    if pad:
        head += f" · {pad[0]}"
    lines = [f'<a href="{link}"><b>{html.escape(c.name)}</b></a>', head, "",
             f"<code>{html.escape(c.address)}</code>", ""]
    if c.price:
        fdv = f"💰 FDV <b>{usd(c.fdv)}</b>" + (f"   🔥 ATH <b>{usd(c.ath_mcap)}</b>" if c.ath_mcap else "")
        lines += [
            f"💵 PRICE <b>{price_fmt(c.price)}</b>" + (" · <i>nearly</i>" if c.src == "nearly" else
                                                     " · <i>on-chain Ref DCL</i>" if c.onchain else ""),
            fdv,
            f"💧 LIQ <b>{usd(c.liq)}</b>",
            f"🔄 VOL 24H <b>{usd(c.vol24)}</b> · TXNS <b>{c.txns24}</b>" if c.src != "dcl" else
            "🔄 VOL / TXNS: <i>after DexScreener indexes the pool</i>",
            f"📊 1H <b>{pct_fmt(c.chg.get('h1', 0))}</b> · 6H <b>{pct_fmt(c.chg.get('h6', 0))}</b>"
            f" · 24H <b>{pct_fmt(c.chg.get('h24', 0))}</b>",
        ]
    else:
        lines.append("📭 No DEX trading found yet")
    if caller:
        lines += [""] + calls.caller_line(caller, c.fdv, html.escape)
    lines.append("")
    if c.holders:
        n = f"{c.holders_total}+" if c.holders_more else str(c.holders_total)
        lines.append(f"👥 <b>{n}</b> holders · TOP-10: <b>{c.top10:.1f}%</b>")
        people = [h for h in c.holders if not h[2]]
        cells = [f'{i}. 👤 <a href="{nb(acc)}">{p:.2f}%</a>' for i, (acc, p, _) in enumerate(people, 1)]
        for i in range(0, len(cells), 2):
            lines.append("   ".join(cells[i:i + 2]))
        pools = [h for h in c.holders if h[2]]
        if pools:
            lines.append("🏊 Pools: " + " · ".join(
                f'<a href="{nb(acc)}">{html.escape(acc if len(acc) <= 24 else acc[:21] + "…")}</a> {p:.2f}%'
                for acc, p, _ in pools))
    else:
        lines.append("👥 Holders: no data")
    return "\n".join(lines)


def card_keyboard(c: TokenCard, has_call: bool = False) -> InlineKeyboardMarkup:
    row = []
    if c.pair_url:
        row.append(InlineKeyboardButton("📈 DexScreener", url=c.pair_url))
    pad = pad_of(c.address)
    if pad:
        row.append(InlineKeyboardButton(pad[0], url=pad[1].format(a=c.address)))
    row.append(InlineKeyboardButton("👥 Holders", url=f"https://nearblocks.io/token/{c.address}#tokenHolders"))
    key = store.scan_key(c.address)
    second = [InlineKeyboardButton("🔄 Refresh", callback_data="rf:" + key)]
    if has_call:
        second.append(InlineKeyboardButton("📊 PnL", callback_data="pnl:" + key))
    buy = [InlineKeyboardButton("🟢 Buy on Bettear", url=deploys.buy_link(c.address))]
    return InlineKeyboardMarkup([buy, row, second])


def visible_len(s: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


async def build_card(update: Update, address: str, make_call: bool = False):
    """make_call=True only when someone dropped the contract in a group (not for buttons / private chats)."""
    c = await scanner.card(address)
    if c is None:
        return None, None, None, None
    chat, u = update.effective_chat, update.effective_user
    calls.update_mcap(store.db, address, c.fdv, c.candles, c.supply)
    caller = calls.get_call(store.db, chat.id, address)
    if caller is None and make_call and chat.type != ChatType.PRIVATE and u and not u.is_bot:
        caller = calls.record_call(store.db, chat.id, address, u.id, u.first_name or "?", u.username or "",
                                   c.symbol, c.fdv)
    png = await asyncio.to_thread(render_chart, c) if c.candles else None
    return c, fmt_card(c, caller), card_keyboard(c, caller is not None), png


async def send_card(update: Update, ctx, address: str, quiet: bool, make_call: bool = False):
    msg = update.effective_message
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING,
                                   message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
    c, text, kb, png = await build_card(update, address, make_call)
    if c is None:
        if not quiet:  # private chat or /scan: not a token → maybe a wallet
            await send_wallet(update, ctx, address)
        return
    if png and visible_len(text) <= 1024:
        await msg.reply_photo(png, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
    else:
        if png:
            await msg.reply_photo(png)
        await msg.reply_html(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


async def near_usd() -> float:
    try:
        s = await nearprice.stats(http)
        return float((s or {}).get("current_price") or 0)
    except Exception:
        return 0.0


PERIODS = {"7d": "7 days", "30d": "30 days", "all": "All time"}


def wallet_keyboard(acc: str, period: str = "all") -> InlineKeyboardMarkup:
    key = store.scan_key(acc)
    per = [InlineKeyboardButton(("✅ " if p == period else "") + {"7d": "7D", "30d": "30D", "all": "All"}[p],
                                callback_data=f"wp:{key}:{p}") for p in PERIODS]
    return InlineKeyboardMarkup([per,
                                 [InlineKeyboardButton("🖼 PnL card", callback_data=f"wc:{key}:{period}")],
                                 [InlineKeyboardButton("🔄 Refresh", callback_data=f"wl:{key}:{period}"),
                                  InlineKeyboardButton("🌐 Nearblocks", url=f"https://nearblocks.io/address/{acc}")]])


async def send_wallet(update: Update, ctx, acc: str):
    msg = update.effective_message
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING,
                                   message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
    try:
        text, _ = await wallets.report(acc, await near_usd(), "all")
    except Exception:
        log.exception("wallet report %s", acc)
        text = "⚠️ Couldn't build the wallet report, try again in a minute."
        await msg.reply_html(text)
        return
    if text is None:
        await msg.reply_html(f"<code>{html.escape(acc)}</code> is neither a token nor an existing NEAR account.")
        return
    await msg.reply_html(text, reply_markup=wallet_keyboard(acc, "all"), link_preview_options=NO_PREVIEW)


async def cmd_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    address, _ = extract_address(" ".join(ctx.args or []))
    if not address:
        await update.effective_message.reply_html("Usage: <code>/wallet alice.near</code>")
        return
    await send_wallet(update, ctx, address)


def _wallet_cb(data: str):
    parts = data.split(":")
    acc = store.token_by_key(parts[1]) if len(parts) > 1 else None
    period = parts[2] if len(parts) > 2 and parts[2] in PERIODS else "all"
    return acc, period


async def on_wallet_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Period buttons (wp:) and Refresh (wl:) — redraw the same message."""
    q = update.callback_query
    acc, period = _wallet_cb(q.data)
    if not acc:
        await q.answer("Send the wallet address again.", show_alert=True)
        return
    if q.data.startswith("wl:"):
        wallets._cache.pop(acc, None)  # Refresh → fetch fresh data
    await q.answer("Refreshing…" if q.data.startswith("wl:") else PERIODS[period])
    try:
        text, _ = await wallets.report(acc, await near_usd(), period)
        if text:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=wallet_keyboard(acc, period),
                                      link_preview_options=NO_PREVIEW)
    except Exception as ex:  # "message is not modified" when nothing changed — fine
        log.info("wallet period: %s", ex)


async def on_wallet_card(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🖼 PnL card — picture for the selected period."""
    q = update.callback_query
    acc, period = _wallet_cb(q.data)
    if not acc:
        await q.answer("Send the wallet address again.", show_alert=True)
        return
    await q.answer("Drawing the card…")
    try:
        _, st = await wallets.report(acc, await near_usd(), period)
    except Exception:
        log.exception("wallet card %s", acc)
        st = None
    if not st or st.get("empty"):
        await q.message.reply_text("No swaps in this period — nothing to draw.")
        return
    png = await asyncio.to_thread(pnl_art.render_wallet, acc, PERIODS[period], st["P"], st["pct"], st["B"],
                                  st["S"], st["H"], st["wins"], st["n"], st.get("best_label", ""), st["points"])
    await q.message.reply_photo(png, caption=f"👛 <code>{html.escape(acc)}</code> · {PERIODS[period]} PnL",
                                parse_mode=ParseMode.HTML)


async def do_scan(update: Update, ctx, text: str, quiet: bool):
    address, ref = extract_address(text)
    if ref:
        address = await scanner.resolve_pair(ref)
        if not address:
            if not quiet:
                await update.effective_message.reply_text("Couldn't find a pair for this link.")
            return
    if not address:
        if not quiet:
            await update.effective_message.reply_html(
                "Send a NEAR token address or link, e.g.:\n<code>/scan raven.nearlytrade.near</code>")
        return
    ensure_pad_token(address)  # any memepad token someone scans joins /top and the MC/dip watch
    await send_card(update, ctx, address, quiet, make_call=True)


async def cmd_padadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner: /padadd token.nearlytrade.near [more…] — force-add memepad tokens to /top and alerts."""
    if not is_owner(update):
        return
    msg = update.effective_message
    if not ctx.args:
        await msg.reply_html("Usage: <code>/padadd justai.nearlytrade.near</code>")
        return
    out = []
    for a in ctx.args:
        a = a.strip().lower()
        if pad_of(a) is None:
            out.append(f"❌ <code>{html.escape(a)}</code> — not a nearly / JustHoot token")
        elif ensure_pad_token(a):
            out.append(f"✅ <code>{html.escape(a)}</code> added")
        else:
            out.append(f"• <code>{html.escape(a)}</code> already watched")
    await msg.reply_html("\n".join(out))


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await do_scan(update, ctx, " ".join(ctx.args), quiet=False)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    address, ref = extract_address(text)
    if address or ref:  # in groups stay silent if the address is not a token (could be a wallet)
        await do_scan(update, ctx, text, quiet=update.effective_chat.type != ChatType.PRIVATE)


async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    address = store.token_by_key(q.data.split(":", 1)[1])
    c, text, kb, png = await build_card(update, address) if address else (None,) * 4
    if c is None:
        await q.answer("Please send the token address again.", show_alert=True)
        return
    try:
        if q.message.photo and png and visible_len(text) <= 1024:
            await q.edit_message_media(InputMediaPhoto(png, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
        elif q.message.photo:
            await q.edit_message_reply_markup(reply_markup=kb)
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, link_preview_options=NO_PREVIEW)
    except Exception as e:  # e.g. "message is not modified"
        log.info("refresh: %s", e)
    await q.answer("Updated")


async def on_scan_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """🔎 Scan under an alert → full token card as a reply in the same topic."""
    q = update.callback_query
    address = store.token_by_key(q.data.split(":", 1)[1])
    if not address:
        await q.answer("Token not found.", show_alert=True)
        return
    await q.answer("Scanning…")
    await send_card(update, ctx, address, quiet=False)


async def on_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """📊 PnL → card image of the call in this chat."""
    q = update.callback_query
    address = store.token_by_key(q.data.split(":", 1)[1])
    call = calls.get_call(store.db, update.effective_chat.id, address) if address else None
    if not call:
        await q.answer("No call for this token in this chat yet.", show_alert=True)
        return
    c = await scanner.card(address)
    if c and c.fdv > 0:
        calls.update_mcap(store.db, address, c.fdv, c.candles, c.supply)
        call = calls.get_call(store.db, update.effective_chat.id, address)
    if call.mcap <= 0:
        await q.answer("Call is saved, but there's no price yet — PnL starts at the first trade.", show_alert=True)
        return
    await q.answer("Building PnL card…")
    now = c.fdv if c else call.last_mcap
    if c:
        calls.update_mcap(store.db, address, c.fdv, c.candles, c.supply)
        call = calls.get_call(store.db, update.effective_chat.id, address)
    png = await asyncio.to_thread(calls.render_pnl, call, c.name if c else "", now, c.candles if c else None,
                                  c.supply if c else 0, update.effective_chat.title or "")
    who = f"@{call.username}" if call.username else call.user
    pct = (now / call.mcap - 1) * 100 if call.mcap else 0
    cap = (f"📊 <b>${html.escape(call.sym)}</b> called by <b>{html.escape(who)}</b> {calls.ago(call.ts)} ago\n"
           f"{usd(call.mcap)} → {usd(now)}  <b>{pct:+.1f}%</b> · 🔝 ATH <b>{calls.xfmt(call.x_peak)}</b>")
    await q.message.reply_photo(png, caption=cap, parse_mode=ParseMode.HTML)


def top_keyboard(active: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("• " if p == active else "") + p.upper(),
                                                       callback_data="top:" + p) for p in calls.PERIODS]])


async def cmd_callers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/callers [24h|7d|30d|all] — best callers of this chat."""
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Use /callers in a group — calls are counted per group.")
        return
    period = (ctx.args[0].lower() if ctx.args else "7d")
    period = period if period in calls.PERIODS else "7d"
    await update.effective_message.reply_html(calls.leaderboard(store.db, update.effective_chat.id, period, html.escape),
                                              reply_markup=top_keyboard(period), link_preview_options=NO_PREVIEW)


async def on_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    period = q.data.split(":", 1)[1]
    try:
        await q.edit_message_text(calls.leaderboard(store.db, update.effective_chat.id, period, html.escape),
                                  parse_mode=ParseMode.HTML, reply_markup=top_keyboard(period),
                                  link_preview_options=NO_PREVIEW)
    except Exception as e:
        log.info("top: %s", e)
    await q.answer()


# ---------- /top /topmc /topdex ----------
def top_link(r: dict) -> str:
    pad = pad_of(r["address"])
    return pad[1].format(a=r["address"]) if pad else f"https://dexscreener.com/{CHAIN}/{r['address']}"


def list_keyboard(active: str) -> InlineKeyboardMarkup:
    names = {"vol": "📊 Volume", "mc": "💰 MCap", "hot": "🔥 Trending"}
    return InlineKeyboardMarkup([[InlineKeyboardButton(("• " if k == active else "") + v, callback_data="tl:" + k)
                                  for k, v in names.items()]])


async def top_text(kind: str) -> str:
    if kind == "hot":
        # candidates: GeckoTerminal trending + top-volume pools, memepad tokens, called tokens → DexScreener data
        tokens = await tops.gt_candidates(http)
        tokens += [r[0] for r in store.q("SELECT address FROM pad_tokens")] + calls.recent_tokens(store.db, 7)
        tokens = [t for t in dict.fromkeys(tokens) if tops.is_meme(t, "")]
        pairs = []
        for i in range(0, len(tokens), 30):
            data = await ds_get(f"/tokens/v1/{CHAIN}/" + ",".join(tokens[i:i + 30]))
            pairs += data if isinstance(data, list) else []
        rows = tops.hot_rows(pairs)
    else:
        if not store.q("SELECT 1 FROM pad_tokens LIMIT 1"):
            await discover_pad_tokens(None)
        add_extra_pads()
        tokens = [r[0] for r in store.q("SELECT address FROM pad_tokens")]
        rows = await digest.collect(ds_get, CHAIN, tokens, min_liq=300 if kind == "mc" else 0, min_vol=1)
        rows = await add_nearly_rows(rows, kind)
        rows.sort(key=lambda r: r["vol"] if kind == "vol" else r["fdv"], reverse=True)
    return tops.fmt(kind, rows, top_link, lambda a: (pad_of(a) or ("",))[0])


NEARLY_TOP_MAX = int(os.environ.get("NEARLY_TOP_MAX", "25"))


async def add_nearly_rows(rows: list[dict], kind: str) -> list[dict]:
    """nearly.trade tokens still trading on the memepad have no DexScreener pair (or a stale one) — take their
    24h volume / MC / change from nearly.trade's own API. Checked: PAD_EXTRA tokens always, plus recent nearly
    tokens missing from DexScreener (newest first, capped by NEARLY_TOP_MAX)."""
    have = {r["address"]: r for r in rows}
    since = time.time() - ONCHAIN_DAYS * 86400
    recent = [r[0] for r in store.q("SELECT address FROM pad_tokens WHERE first_seen>=? ORDER BY first_seen DESC", (since,))]
    check = list(dict.fromkeys([a for a in PAD_EXTRA if a.endswith(".nearlytrade.near")] +
                               [a for a in recent if a.endswith(".nearlytrade.near") and a not in have]))[:NEARLY_TOP_MAX]
    sem = asyncio.Semaphore(6)

    async def one(addr):
        async with sem:
            try:
                return addr, await scanner.onchain_pair(addr)
            except Exception as e:
                log.info("nearly top %s: %s", addr, e)
                return addr, None

    for addr, p in await asyncio.gather(*(one(a) for a in check)):
        if not p or not p.get("_onchain") or "nearly.trade" not in (p.get("url") or ""):
            continue
        vol = tops.f((p.get("volume") or {}).get("h24"))
        liq = tops.f((p.get("liquidity") or {}).get("usd"))
        if vol < 1 or (kind == "mc" and liq < 300):
            continue
        b = p.get("baseToken") or {}
        row = dict(address=addr, symbol=b.get("symbol") or addr.split(".")[0].upper(), name=b.get("name") or "",
                   change=tops.f((p.get("priceChange") or {}).get("h24")), fdv=tops.f(p.get("fdv") or p.get("marketCap")),
                   vol=vol, liq=liq, url=p.get("url") or "", icon=(p.get("info") or {}).get("imageUrl"))
        old = have.get(addr)
        if old is None:
            rows.append(row)
            have[addr] = row
        elif row["vol"] > old["vol"]:  # nearly.trade sees memepad trades DexScreener misses
            old.update(vol=row["vol"], fdv=row["fdv"] or old["fdv"], change=row["change"])
    return rows


async def cmd_topcheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner: /topcheck token — why a token is / isn't in /top."""
    if not is_owner(update):
        return
    msg = update.effective_message
    a = (ctx.args[0] if ctx.args else "justai.nearlytrade.near").strip().lower()
    lines = [f"🔎 <code>{html.escape(a)}</code>",
             "pad_tokens: " + ("✅ yes" if store.q("SELECT 1 FROM pad_tokens WHERE address=?", (a,)) else "❌ no"),
             "PAD_EXTRA: " + ("✅ yes" if a in PAD_EXTRA else "—")]
    ds = await digest.collect(ds_get, CHAIN, [a], min_liq=0, min_vol=0)
    if ds:
        r = ds[0]
        lines.append(f"DexScreener: ✅ vol {usd(r['vol'])} · MC {usd(r['fdv'])} · {r['change']:+.0f}%")
    else:
        lines.append("DexScreener: ❌ no pair with 24h data")
    try:
        p = await scanner.onchain_pair(a)
    except Exception as e:
        p = None
        lines.append(f"nearly.trade: ⚠️ {html.escape(str(e))[:120]}")
    if p and "nearly.trade" in (p.get("url") or ""):
        lines.append(f"nearly.trade: ✅ vol {usd(tops.f((p.get('volume') or {}).get('h24')))} · "
                     f"MC {usd(tops.f(p.get('fdv')))} · liq {usd(tops.f((p.get('liquidity') or {}).get('usd')))}")
    elif p is None:
        lines.append("nearly.trade: ❌ no data")
    else:
        lines.append("nearly.trade: — (price from on-chain pool, no memepad stats)")
    await msg.reply_html("\n".join(lines))


async def send_top(update: Update, kind: str):
    msg = update.effective_message
    await update.get_bot().send_chat_action(update.effective_chat.id, ChatAction.TYPING,
                                            message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
    await msg.reply_html(await top_text(kind), reply_markup=list_keyboard(kind), link_preview_options=NO_PREVIEW)


async def cmd_top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_top(update, "vol")


async def cmd_topmc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_top(update, "mc")


async def cmd_topdex(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_top(update, "hot")


async def on_toplist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    kind = q.data.split(":", 1)[1]
    await q.answer("Loading…")
    try:
        await q.edit_message_text(await top_text(kind), parse_mode=ParseMode.HTML, reply_markup=list_keyboard(kind),
                                  link_preview_options=NO_PREVIEW)
    except Exception as e:
        log.info("toplist: %s", e)


xw = xwatch.XWatcher()


async def poll_x(ctx: ContextTypes.DEFAULT_TYPE):
    if not store.topic("x"):
        return
    for tw in await xw.poll(store.db, store.seen, store.mark, store.kv_get, store.kv_set):
        text, photo, link = xwatch.fmt(tw)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open on 𝕏", url=link)]])
        await post(ctx, "x", text, photo, kb)
        log.info("X post %s", tw.get("id"))


async def cmd_xadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not ctx.args:
        await update.effective_message.reply_html("Usage: <code>/xadd ilblackdragon</code> (several: space-separated)")
        return
    added = [u for u in (xwatch.add(store.db, a) for a in ctx.args) if u]
    note = "" if xwatch.KEY else "\n\n⚠️ TWITTERAPI_KEY is not set in .env yet — posts won't arrive until it is."
    if not store.topic("x"):
        note += "\n⚠️ Bind a topic first: <code>/settopic x</code> inside the topic."
    await update.effective_message.reply_html("✅ Watching: " + ", ".join(f"@{html.escape(u)}" for u in added) + note)


async def cmd_xdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not ctx.args:
        return
    gone = [a for a in ctx.args if xwatch.remove(store.db, a)]
    await update.effective_message.reply_html("Removed: " + (", ".join(html.escape(g) for g in gone) or "nothing"))


async def cmd_xlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    users = xwatch.accounts(store.db)
    await update.effective_message.reply_html(
        ("𝕏 Watching:\n" + "\n".join(f"• @{html.escape(u)}" for u in users)) if users else
        "No X accounts yet. Add with <code>/xadd ilblackdragon</code>", link_preview_options=NO_PREVIEW)


async def post_digest(ctx: ContextTypes.DEFAULT_TYPE):
    """Daily 24h gainers / losers among memepad tokens and tokens called in groups."""
    if not store.topic("daily"):
        return False
    tokens = [r[0] for r in store.q("SELECT address FROM pad_tokens")] + calls.recent_tokens(store.db, 30)
    rows = await digest.collect(ds_get, CHAIN, tokens)
    up, down = digest.pick(rows)
    if not up and not down:
        log.info("Digest: nothing to post")
        return False

    def link(r):
        pad = pad_of(r["address"])
        return pad[1].format(a=r["address"]) if pad else (r["url"] or f"https://dexscreener.com/{CHAIN}/{r['address']}")

    icons = await digest.fetch_icons(up + down)
    from datetime import datetime
    from zoneinfo import ZoneInfo
    date_str = datetime.now(ZoneInfo(DIGEST_TZ)).strftime("%d %b %Y")
    png = await asyncio.to_thread(digest.render, up, down, icons, date_str)
    await post(ctx, "daily", digest.text(up, down, link), png)
    log.info("Digest posted: %d up, %d down", len(up), len(down))
    return True


async def cmd_digest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not store.topic("daily"):
        await update.effective_message.reply_html("Bind a topic first: <code>/settopic daily</code> inside the topic.")
        return
    ok = await post_digest(ctx)
    if not ok:
        await update.effective_message.reply_text("No tokens with enough liquidity/volume yet — try later.")


def near_keyboard(active: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(("• " if p == active else "") + p.upper(), callback_data="nr:" + p)
                                  for p in nearprice.PERIODS] +
                                 [InlineKeyboardButton("🔄", callback_data="nr:" + active)]])


async def near_card(period: str):
    s = await nearprice.stats(http)
    rows = await nearprice.candles(http, period)
    if not s:
        return None, None
    png = await asyncio.to_thread(nearprice.render, s, rows, period)
    return nearprice.caption(s, period), png


async def cmd_near(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/near [1d|7d|30d] — live $NEAR price and chart."""
    period = ctx.args[0].lower() if ctx.args and ctx.args[0].lower() in nearprice.PERIODS else "1d"
    text, png = await near_card(period)
    msg = update.effective_message
    if not text:
        await msg.reply_text("Price service is not responding, try again in a minute.")
    elif png:
        await msg.reply_photo(png, caption=text, parse_mode=ParseMode.HTML, reply_markup=near_keyboard(period))
    else:
        await msg.reply_html(text, reply_markup=near_keyboard(period))


async def on_near(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    period = q.data.split(":", 1)[1]
    text, png = await near_card(period)
    if not text:
        await q.answer("Price service is not responding.", show_alert=True)
        return
    try:
        if q.message.photo and png:
            await q.edit_message_media(InputMediaPhoto(png, caption=text, parse_mode=ParseMode.HTML),
                                       reply_markup=near_keyboard(period))
        else:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=near_keyboard(period))
    except Exception as e:
        log.info("near refresh: %s", e)
    await q.answer()


async def refresh_calls(ctx: ContextTypes.DEFAULT_TYPE):
    """Every few minutes: current market cap for tokens called in the last 30 days (keeps ATH since call fresh)."""
    tokens = calls.recent_tokens(store.db)
    for i in range(0, len(tokens), 30):
        data = await ds_get(f"/tokens/v1/{CHAIN}/" + ",".join(tokens[i:i + 30]))
        best: dict[str, dict] = {}
        for p in data if isinstance(data, list) else []:
            addr = (p.get("baseToken") or {}).get("address")
            if addr in tokens and (addr not in best or ((p.get("liquidity") or {}).get("usd") or 0) >
                                   ((best[addr].get("liquidity") or {}).get("usd") or 0)):
                best[addr] = p
        for addr, p in best.items():
            try:
                mc = float(p.get("fdv") or p.get("marketCap") or 0)
            except (TypeError, ValueError):
                continue
            calls.update_mcap(store.db, addr, mc)
        for addr in tokens[i:i + 30]:
            if addr not in best and scanner.has_dcl_pool(addr) is not False:  # fresh migration → on-chain pool price
                try:
                    calls.update_mcap(store.db, addr, await scanner.onchain_mcap(addr))
                except Exception:
                    log.exception("onchain mcap %s", addr)


# ---------- group / topic commands ----------
WELCOME = ("👋 <b>NEAR Analyzer</b>\n\n"
           "Just paste a NEAR token contract and get a full analysis: price, market cap, ATH, liquidity, "
           "volume, top holders and chart.\n\n"
           "Example: <code>raven.nearlytrade.near</code>\n"
           "Links from nearly, JustHoot and DexScreener work too.\n\n"
           "👛 Paste a wallet (e.g. <code>alice.near</code>) to see its trading PnL — total and 7 days, "
           "win rate, best and worst tokens. In groups: <code>/wallet alice.near</code>\n\n"
           "🎯 In groups the first person to drop a contract gets the call — tap 📊 PnL under the card "
           "to see how it did, and /callers for the best callers.\n\n"
           "📈 /near — live $NEAR price and chart\n"
           "📊 /top — top 10 memepad tokens by 24h volume\n"
           "💰 /topmc — top 10 by market cap\n"
           "🔥 /topdex — top 10 trending on NEAR")

ADMIN_HELP = ("🛠 <b>Admin panel</b>\n\n"
              "Alerts in group topics: add me to a forum group as admin, open a topic and send "
              "<code>/settopic dexpaid</code>\n\nTopic keys:\n" +
              "\n".join(f"<code>{k}</code> — {v}" for k, v in TOPICS.items()) +
              "\n\n/topics — show bindings\n/unsettopic &lt;key&gt; — remove\n/test &lt;key&gt; — test post\n"
              "\n\n𝕏 <b>X posts</b>: /xadd &lt;username&gt; · /xdel &lt;username&gt; · /xlist "
              "(needs TWITTERAPI_KEY in .env and <code>/settopic x</code>)\n"
              "📅 <b>Daily digest</b>: <code>/settopic daily</code>, posts every day at " + DIGEST_TIME +
              " (" + DIGEST_TZ + "); /digest — post now\n"
              "🚀 <b>New deploys channel</b>: add me to the channel as admin, then here: "
              "<code>/setdeploys @channel</code>; /deploys — status; "
              "<code>/deploytest token.nearlytrade.near</code> — preview a post here; "
              "<code>/deploypost token</code> — post a missed deploy to the channel; "
              "/deploysnow — skip the backlog, read from now\n"
              "📊 <code>/padadd token.nearlytrade.near</code> — force a memepad token into /top and MC/dip alerts\n"
              "🩺 <code>/topcheck token</code> — why a token is / isn't in /top\n"
              "/admin — this panel")


def owner_id() -> int | None:
    v = os.environ.get("OWNER_ID") or store.kv_get("owner")
    return int(v) if v and v.lstrip("-").isdigit() else None


def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == owner_id()


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    # The first person to /start the bot in private becomes its owner (unless OWNER_ID is set in .env)
    if owner_id() is None and update.effective_chat.type == ChatType.PRIVATE and update.effective_user:
        store.kv_set("owner", str(update.effective_user.id))
        log.info("Owner set to %s", update.effective_user.id)
    if ctx.args and ctx.args[0].startswith("sc_"):  # 🔎 Scan button from the deploys channel
        address = store.token_by_key(ctx.args[0][3:])
        if address:
            await send_card(update, ctx, address, quiet=False)
            return
    await msg.reply_html(WELCOME, link_preview_options=NO_PREVIEW)
    if is_owner(update) and update.effective_chat.type == ChatType.PRIVATE:
        await msg.reply_html(ADMIN_HELP, link_preview_options=NO_PREVIEW)


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.effective_message.reply_html(ADMIN_HELP, link_preview_options=NO_PREVIEW)


async def cmd_settopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_owner(update):
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        await msg.reply_text("Use this command inside a topic of your group.")
        return
    if not ctx.args or ctx.args[0].lower() not in TOPICS:
        await msg.reply_html("Usage: <code>/settopic dexpaid</code>\nKeys: " + ", ".join(TOPICS))
        return
    key = ctx.args[0].lower()
    thread = msg.message_thread_id if msg.is_topic_message else None
    store.set_topic(key, update.effective_chat.id, thread)
    where = "this topic" if thread else "the General chat"
    await msg.reply_html(f"✅ <b>{html.escape(TOPICS[key])}</b> alerts will be posted in {where}.")


async def cmd_unsettopic(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update) or not ctx.args:
        return
    store.q("DELETE FROM topics WHERE key=?", (ctx.args[0].lower(),))
    await update.effective_message.reply_html(f"Removed <code>{html.escape(ctx.args[0])}</code>.")


async def cmd_topics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    rows = store.q("SELECT key, thread_id FROM topics WHERE chat_id=?", (update.effective_chat.id,))
    text = "\n".join(f"<code>{k}</code> → topic {t or 'General'}" for k, t in rows) or \
        "No topics bound yet. Use /settopic inside a topic."
    await update.effective_message.reply_html(text)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    key = ctx.args[0].lower() if ctx.args else "dexpaid"
    if not store.topic(key):
        await update.effective_message.reply_html(f"Topic <code>{html.escape(key)}</code> is not bound.")
        return
    await post(ctx, key, f"🧪 Test alert for <b>{html.escape(TOPICS.get(key, key))}</b>")


# ---------- new deploys channel ----------
async def cmd_setdeploys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not is_owner(update):
        return
    if not ctx.args:
        await msg.reply_html("Usage: <code>/setdeploys @yourchannel</code> (or the channel id -100…)\n"
                             "<code>/setdeploys off</code> — stop posting")
        return
    arg = ctx.args[0].strip()
    if arg.lower() == "off":
        store.kv_set("deploy_channel", "")
        await msg.reply_text("Deploy posts stopped.")
        return
    if "t.me/" in arg:
        arg = "@" + arg.rstrip("/").split("/")[-1]
    try:
        chat = await ctx.bot.get_chat(arg)
        me = await ctx.bot.get_chat_member(chat.id, ctx.bot.id)
    except Exception as e:
        await msg.reply_html(f"Can't open <code>{html.escape(arg)}</code>: {html.escape(str(e))}\n"
                             "Add the bot to the channel as admin first.")
        return
    if me.status not in ("administrator", "creator") or getattr(me, "can_post_messages", True) is False:
        await msg.reply_text("The bot must be an admin of the channel with the right to post messages.")
        return
    store.kv_set("deploy_channel", str(chat.id))
    await msg.reply_html(f"✅ New deploys will be posted to <b>{html.escape(chat.title or arg)}</b>.")


async def cmd_deploys(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.effective_message.reply_html(feed.status(), link_preview_options=NO_PREVIEW)


async def cmd_deploytest(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Preview a deploy post for any memepad token, sent here (not to the channel)."""
    msg = update.effective_message
    if not is_owner(update):
        return
    address, _ = extract_address(" ".join(ctx.args or []))
    if not address:
        await msg.reply_html("Usage: <code>/deploytest ganados.nearlytrade.near</code>")
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text, image, kb = await feed.build(address, ctx.bot.username)
    if text is None:
        await msg.reply_text("Not a token (no ft_metadata).")
        return
    await feed.send(ctx.bot, update.effective_chat.id, text, image, kb)


async def cmd_deploysnow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Forget the backlog: start reading deploys from the current block."""
    if not is_owner(update):
        return
    feed.skip_to_now = True
    await update.effective_message.reply_text("⏩ Skipping old blocks — reading new deploys from now on.")


async def cmd_deploypost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Post a (missed) deploy to the deploys channel by hand."""
    msg = update.effective_message
    if not is_owner(update):
        return
    address, _ = extract_address(" ".join(ctx.args or []))
    ch = feed.channel()
    if not address or not ch:
        await msg.reply_html("Usage: <code>/deploypost fde-2.nearlytrade.near</code>" +
                             ("" if ch else "\nFirst set the channel: /setdeploys @channel"))
        return
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    text, image, kb = await feed.build(address, ctx.bot.username)
    if text is None:
        await msg.reply_text("Not a token (no ft_metadata).")
        return
    await feed.send(ctx.bot, ch, text, image, kb)
    store.q("UPDATE deploys SET posted=1 WHERE token=?", (address,))
    await msg.reply_text("✅ Posted to the channel.")


async def on_startup(app: Application):
    app.create_task(feed.follow(app.bot))
    if feed.source == "intear":
        app.create_task(feed.follow_intear(app.bot))


async def on_shutdown(app: Application):
    await scanner.close()
    await xw.close()
    await http.aclose()


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set (put it in .env)")
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    for name, fn in [("start", cmd_start), ("help", cmd_start), ("admin", cmd_admin), ("scan", cmd_scan), ("settopic", cmd_settopic),
                     ("unsettopic", cmd_unsettopic), ("topics", cmd_topics), ("test", cmd_test),
                     ("top", cmd_top), ("topmc", cmd_topmc), ("topdex", cmd_topdex), ("callers", cmd_callers), ("xadd", cmd_xadd), ("xdel", cmd_xdel), ("xlist", cmd_xlist), ("digest", cmd_digest), ("near", cmd_near),
                     ("setdeploys", cmd_setdeploys), ("deploys", cmd_deploys), ("deploytest", cmd_deploytest), ("deploypost", cmd_deploypost), ("deploysnow", cmd_deploysnow), ("wallet", cmd_wallet), ("padadd", cmd_padadd), ("topcheck", cmd_topcheck)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^rf:"))
    app.add_handler(CallbackQueryHandler(on_wallet_period, pattern=r"^w[pl]:"))
    app.add_handler(CallbackQueryHandler(on_wallet_card, pattern=r"^wc:"))
    app.add_handler(CallbackQueryHandler(on_scan_button, pattern=r"^sc:"))
    app.add_handler(CallbackQueryHandler(on_pnl, pattern=r"^pnl:"))
    app.add_handler(CallbackQueryHandler(on_top, pattern=r"^top:"))
    app.add_handler(CallbackQueryHandler(on_near, pattern=r"^nr:"))
    app.add_handler(CallbackQueryHandler(on_toplist, pattern=r"^tl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(watcher, interval=POLL_SECONDS, first=5)
    app.job_queue.run_repeating(check_memepads, interval=MEMEPAD_POLL_SECONDS, first=15)
    app.job_queue.run_repeating(refresh_calls, interval=300, first=60)
    app.job_queue.run_repeating(poll_x, interval=xwatch.POLL_SECONDS, first=20)
    app.job_queue.run_repeating(feed.track_ath, interval=300, first=90)
    app.job_queue.run_repeating(feed.retry_unposted, interval=60, first=60)
    import datetime as dt
    from zoneinfo import ZoneInfo
    hh, mm = (int(x) for x in DIGEST_TIME.split(":"))
    app.job_queue.run_daily(post_digest, time=dt.time(hh, mm, tzinfo=ZoneInfo(DIGEST_TZ)))
    log.info("NEAR Analyzer starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
