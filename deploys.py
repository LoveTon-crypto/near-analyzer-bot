"""New token deploys on NEAR memepads (nearly / JustHoot) → Telegram channel, like TON "TOPBLAST DEPLOY!" posts.

How it works:
  • Reads every NEAR block from neardata.xyz (free, no key) and catches the moment a memepad factory
    creates a new token account: X.nearlytrade.near, X.launchpad.justhoot.near, X.v2-launchpad.justhoot.near.
  • Dev = the account that signed the deploy transaction.
  • Post: token icon, name / ticker / description, dev's NEAR balance, how much of the token the dev holds,
    the dev's previous deploys and the best ATH market cap any of them reached.
  • Button "Buy" opens Bettear with the owner's referral code already in the link.

Dev history is stored in the bot's database from the moment the feed starts. BACKFILL_DAYS=N in .env
also scans the last N days of blocks once in the background, so older deploys are counted too.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import re
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ParseMode

from scanner import usd

log = logging.getLogger("deploys")

NEARDATA = os.environ.get("NEARDATA_URL", "https://mainnet.neardata.xyz").rstrip("/")
RPC_URLS = ["https://rpc.mainnet.fastnear.com", "https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"]
DEXS = "https://api.dexscreener.com"

# factory account -> (label in the post, token page link)
FACTORIES = {
    "nearlytrade.near": ("NEARLY", "https://nearly.trade/{a}"),
    "launchpad.justhoot.near": ("JUSTHOOT", "https://justhoot.fun/p/{a}"),
    "v2-launchpad.justhoot.near": ("JUSTHOOT", "https://justhoot.fun/p/{a}"),
}
for _f in filter(None, (x.strip() for x in os.environ.get("DEPLOY_EXTRA_FACTORIES", "").split(","))):
    FACTORIES.setdefault(_f, (_f.split(".")[0].upper(), "https://nearblocks.io/token/{a}"))

BETTEAR_BOT = os.environ.get("BETTEAR_BOT", "bettearbot")
BETTEAR_REF = os.environ.get("BETTEAR_REF", "ref-1094156325")
DEPLOY_DELAY = float(os.environ.get("DEPLOY_DELAY", "0"))      # seconds to wait before posting (0 = instantly)
REFRESH_AFTER = int(os.environ.get("DEPLOY_REFRESH", "15"))    # edit the post after N s (dev buy, balance); 0 = off
LATE_REFRESH = int(os.environ.get("DEPLOY_LATE_REFRESH", "120"))  # 2nd edit: socials added on nearly after deploy; 0 = off
OPTIMISTIC = os.environ.get("DEPLOY_OPTIMISTIC", "1") != "0"  # read optimistic blocks (~1-2 s earlier than final)
MAX_LAG_BLOCKS = int(os.environ.get("DEPLOY_MAX_LAG", "20000"))  # ~3 h; below that we catch up, never skip
FASTNEAR_KEY = os.environ.get("FASTNEAR_API_KEY", "").strip()  # optional, lifts neardata rate limits
WORKERS = int(os.environ.get("DEPLOY_WORKERS", "16" if FASTNEAR_KEY else "3"))  # blocks downloaded at once
# without a key neardata can't keep up → don't drown in old blocks: further behind than this → jump to now
KEYLESS_MAX_LAG = int(os.environ.get("DEPLOY_KEYLESS_MAX_LAG", "150"))  # ~1.5 min
PROBE_SLEEP = float(os.environ.get("DEPLOY_PROBE_SLEEP", "0.25"))  # wait between "is the next block out?" checks
# Where new tokens come from: "intear" = free push stream (wss://api.intea.rs/ws), almost no FastNear requests;
# "neardata" = read every block (≈140k FastNear requests/day — far above the free 500k/month).
SOURCE = os.environ.get("DEPLOY_SOURCE", "intear").strip().lower()
INTEAR_WS = os.environ.get("INTEAR_WS", "wss://api.intea.rs/ws")
FALLBACK = os.environ.get("DEPLOY_FALLBACK", "1") != "0"  # Intear down > 90 s → read blocks until it's back
RETRY_WINDOW = 20 * 60  # unposted deploys younger than this are retried
BACKFILL_DAYS = float(os.environ.get("BACKFILL_DAYS", "0"))
ATH_TRACK_DAYS = float(os.environ.get("DEPLOY_ATH_DAYS", "30"))
SECONDS_PER_BLOCK = 0.6
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
START_OK = re.compile(r"^[A-Za-z0-9_=-]{1,64}$")  # ?start= limit: 64 chars; '=' works with Bettear


def factory_of(account: str) -> str | None:
    """X.<factory> where X has no dots → the factory, else None."""
    for fac in FACTORIES:
        if account.endswith("." + fac) and "." not in account[: -len(fac) - 1]:
            return fac
    return None


NDBOT_BOT = os.environ.get("NDBOT_BOT", "NDbot").lstrip("@")   # our own trade bot
NDBOT_REF = os.environ.get("NDBOT_REF", "").strip()             # owner's referral code in NDbot (🤝 Referrals)


def ndbot_link(token: str) -> str:
    """NDbot deep link: opens the token card ready to buy; with the owner's referral code if it fits in 64 chars."""
    t = token.replace(".", "=")
    for param in ((f"r{NDBOT_REF}_c{t}" if NDBOT_REF else ""), f"c{t}", (f"r{NDBOT_REF}" if NDBOT_REF else "")):
        if param and START_OK.match(param):
            return f"https://t.me/{NDBOT_BOT}?start={param}"
    return f"https://t.me/{NDBOT_BOT}"


def buy_buttons(token: str) -> list:
    return [InlineKeyboardButton("🟢 Buy on Bettear", url=buy_link(token)),
            InlineKeyboardButton("⚡ Buy on NDbot", url=ndbot_link(token))]


def buy_link(token: str) -> str:
    """Bettear deep link with the referral code: dots in the contract become '='."""
    param = f"{BETTEAR_REF}-buy-{token.replace('.', '=')}"
    if not START_OK.match(param):  # too long or odd characters → plain referral link
        param = BETTEAR_REF
    return f"https://t.me/{BETTEAR_BOT}?start={param}"


def parse_block(blk: dict) -> list[dict]:
    """Memepad token-account creations in a neardata block."""
    out = []
    try:
        ts = int(((blk.get("block") or {}).get("header") or {}).get("timestamp") or 0) / 1e9
    except (TypeError, ValueError):
        ts = 0
    for shard in blk.get("shards") or []:
        for reo in shard.get("receipt_execution_outcomes") or []:
            r = reo.get("receipt") or {}
            token = r.get("receiver_id") or ""
            fac = factory_of(token)
            if not fac:
                continue
            act = (r.get("receipt") or {}).get("Action")
            if not isinstance(act, dict):
                continue
            actions = act.get("actions") or []
            if not any(a == "CreateAccount" or (isinstance(a, dict) and "CreateAccount" in a) for a in actions):
                continue
            status = ((reo.get("execution_outcome") or {}).get("outcome") or {}).get("status") or {}
            if isinstance(status, dict) and "Failure" in status:
                continue
            out.append({"token": token, "factory": fac, "signer": act.get("signer_id") or "",
                        "predecessor": r.get("predecessor_id") or "", "ts": ts or time.time(),
                        "tx": reo.get("tx_hash") or ""})
    return out


def _tree(rows: list[str]) -> list[str]:
    return [("└ " if i == len(rows) - 1 else "├ ") + row for i, row in enumerate(rows)]


def _visible(s: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def _norm_link(kind: str, v: str) -> str:
    """'@handle', 'x.com/handle', 't.me/chat' → full https URL."""
    v = (v or "").strip()
    if not v or v.startswith("http"):
        return v
    if v.startswith("@"):
        base = {"twitter": "https://x.com/", "telegram": "https://t.me/"}.get(kind)
        return base + v[1:] if base else ""
    if "." in v.split("/")[0]:
        return "https://" + v
    base = {"twitter": "https://x.com/", "telegram": "https://t.me/"}.get(kind)
    return base + v if base else ""


def fmt_amount(x: float) -> str:
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{x / div:.2f}".rstrip("0").rstrip(".") + suf
    return f"{x:,.0f}"


class DeployFeed:
    def __init__(self, store, scanner, http: httpx.AsyncClient):
        self.store = store
        self.scanner = scanner
        self.http = http
        self.head = 0
        self.cursor = 0
        self.last_ok = 0.0
        self.pending: set[str] = set()
        self.misses: list[str] = []
        self.pause_until = 0.0
        self.skip_to_now = False
        self.optimistic = OPTIMISTIC
        self.source = SOURCE
        self.ws_state = "not started"
        self.ws_last = 0.0      # last message from Intear
        self.ws_resume = None   # last height, to resume the subscription
        self.ws_tokens = 0      # memepad tokens received from Intear
        self.stats = {"t0": time.time(), "blocks": 0, "requests": 0, "errors": 0, "limited": 0, "last_err": ""}  # last reasons a deploy wasn't posted (shown in /deploys)
        store.db.executescript("""
            CREATE TABLE IF NOT EXISTS deploys (token TEXT PRIMARY KEY, dev TEXT, factory TEXT, ts REAL,
                                                name TEXT, symbol TEXT, ath REAL DEFAULT 0, posted INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS deploys_dev ON deploys(dev);
        """)
        store.db.commit()

    # ---------- settings ----------
    def channel(self):
        v = self.store.kv_get("deploy_channel") or os.environ.get("DEPLOY_CHANNEL", "")
        if not v:
            return None
        return int(v) if v.lstrip("-").isdigit() else v

    # ---------- NEAR RPC ----------
    async def _rpc(self, params: dict):
        payload = {"jsonrpc": "2.0", "id": "1", "method": "query", "params": params}
        for url in RPC_URLS:
            try:
                r = await self.http.post(url, json=payload, timeout=15)
                data = r.json() if r.status_code < 500 and r.status_code != 429 else None
            except (httpx.HTTPError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if "result" in data:
                return data["result"]
            err = json.dumps(data.get("error") or "")
            if any(x in err for x in ("UNKNOWN_ACCOUNT", "CodeDoesNotExist", "MethodNotFound", "does not exist")):
                return None  # really missing — no point asking another RPC
            # rate limit / lagging node → try the next RPC
        return None

    async def view(self, account: str, method: str, args: dict | None = None, finality: str = "final"):
        res = await self._rpc({"request_type": "call_function", "finality": finality, "account_id": account,
                               "method_name": method,
                               "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()})
        if not res or res.get("error") or "result" not in res:
            return None
        try:
            return json.loads(bytes(res["result"]).decode())
        except ValueError:
            return None

    async def near_balance(self, account: str) -> float | None:
        res = await self._rpc({"request_type": "view_account", "finality": "final", "account_id": account})
        try:
            return int(res["amount"]) / 1e24
        except (TypeError, KeyError, ValueError):
            return None

    # ---------- block reader ----------
    async def _get_json(self, url: str):
        """(ok, data). ok=False → network/server problem, retry later."""
        headers = {"Authorization": f"Bearer {FASTNEAR_KEY}"} if FASTNEAR_KEY else None
        self.stats["requests"] += 1
        try:
            r = await self.http.get(url, follow_redirects=True, timeout=30, headers=headers)
        except httpx.HTTPError as e:
            self.stats["errors"] += 1
            self.stats["last_err"] = f"{type(e).__name__}"
            return False, None
        if r.status_code == 404:
            return True, None
        if r.status_code >= 400:
            self.stats["errors"] += 1
            if r.status_code == 429:
                self.stats["limited"] += 1
                try:
                    wait = float(r.headers.get("retry-after") or 2)
                except ValueError:
                    wait = 2
                self.pause_until = max(self.pause_until, time.time() + min(wait, 30))
            self.stats["last_err"] = f"HTTP {r.status_code}"
            return False, None
        try:
            return True, json.loads(r.content)
        except ValueError:
            self.stats["errors"] += 1
            self.stats["last_err"] = "bad JSON"
            return False, None

    async def final_height(self) -> int:
        kind = "optimistic" if self.optimistic else "final"
        ok, data = await self._get_json(f"{NEARDATA}/v0/last_block/{kind}")
        try:
            return int(data["block"]["header"]["height"]) if ok and data else 0
        except (TypeError, KeyError, ValueError):
            return 0

    async def block(self, h: int):
        if self.optimistic:
            return await self._get_json(f"{NEARDATA}/v0/block_opt/{h}")
        return await self._get_json(f"{NEARDATA}/v0/block/{h}")

    async def _check_optimistic(self):
        """Make sure the optimistic endpoints answer with real blocks; otherwise use final ones."""
        if not self.optimistic:
            return
        h = await self.final_height()
        ok, blk = await self.block(h) if h else (False, None)
        if not (ok and isinstance(blk, dict) and blk.get("block")):
            log.warning("Optimistic blocks unavailable, falling back to final")
            self.optimistic = False

    async def _fetch_retry(self, h: int):
        """Download one block, retrying with backoff until it works."""
        delay = 0.5
        while True:
            pause = self.pause_until - time.time()
            if pause > 0:  # rate-limited: everyone waits instead of hammering
                await asyncio.sleep(pause)
            ok, blk = await self.block(h)
            if ok:
                return blk
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10)

    async def follow(self, bot):
        """Endless loop: a window of blocks is downloaded in parallel (WORKERS at a time) and processed
        strictly in order. A failed block is retried on its own — nothing is skipped or re-downloaded."""
        await asyncio.sleep(5)
        await self._check_optimistic()
        self.cursor = int(self.store.kv_get("deploy_cursor") or 0)
        start_head = 0
        saved = 0
        head_at = 0.0
        tasks: dict[int, asyncio.Task] = {}
        while True:
            try:
                if self.source == "intear" and (self.ws_ok() or not FALLBACK):
                    # Intear delivers deploys → block reader sleeps (re-anchors at the head when needed again)
                    for t in tasks.values():
                        t.cancel()
                    tasks.clear()
                    self.cursor = 0
                    self.head = 0
                    await asyncio.sleep(2)
                    continue
                if not self.head or time.time() - head_at > 5:
                    self.head = await self.final_height() or self.head
                    head_at = time.time()
                    if not self.head:
                        await asyncio.sleep(3)
                        continue
                    if not start_head:
                        start_head = self.head
                        if BACKFILL_DAYS > 0 and not self.store.kv_get("deploy_backfill_done"):
                            asyncio.create_task(self.backfill(start_head))
                    if not self.cursor:
                        self.cursor = self.head
                lag = self.head - self.cursor
                if self.skip_to_now or (not FASTNEAR_KEY and lag > KEYLESS_MAX_LAG):
                    if not self.skip_to_now:
                        self._miss(f"too slow without FASTNEAR_API_KEY, skipped {lag} blocks")
                    self.skip_to_now = False
                    for t in tasks.values():
                        t.cancel()
                    tasks.clear()
                    self.cursor = self.head
                    self.store.kv_set("deploy_cursor", str(self.cursor))
                    continue
                if lag > MAX_LAG_BLOCKS:
                    log.warning("Deploys: %d blocks behind, jumping to the head (deploys in the gap are lost)", lag)
                    self._miss(f"skipped {lag} blocks after long downtime")
                    for t in tasks.values():
                        t.cancel()
                    tasks.clear()
                    self.cursor = self.head - 5
                if self.cursor > self.head:
                    # Caught up. Instead of hammering last_block (that was ~4 extra requests per block),
                    # ask for the next block itself: when it exists we get it in the same request.
                    ok, blk = await self.block(self.cursor)
                    if ok and blk:
                        self.head = self.cursor
                    else:
                        if time.time() - head_at > 2:  # rarely: confirm it's not a skipped height
                            head_at = 0
                        await asyncio.sleep(PROBE_SLEEP)
                        continue
                    for d in parse_block(blk):
                        await self.on_deploy(bot, d, live=True)
                    self.cursor += 1
                    self.stats["blocks"] += 1
                    self.last_ok = time.time()
                    continue
                # keep WORKERS downloads running ahead of the cursor
                for h in range(self.cursor, min(self.head, self.cursor + WORKERS - 1) + 1):
                    if h not in tasks:
                        tasks[h] = asyncio.create_task(self._fetch_retry(h))
                blk = await tasks.pop(self.cursor)
                if blk:
                    for d in parse_block(blk):
                        await self.on_deploy(bot, d, live=True)
                self.cursor += 1
                self.stats["blocks"] += 1
                self.last_ok = time.time()
                if self.cursor - saved >= 20:
                    saved = self.cursor
                    self.store.kv_set("deploy_cursor", str(self.cursor))
            except asyncio.CancelledError:
                for t in tasks.values():
                    t.cancel()
                raise
            except Exception:
                log.exception("Deploys loop error")
                await asyncio.sleep(5)

    def _miss(self, why: str):
        log.warning("Deploy miss: %s", why)
        self.misses = (self.misses + [time.strftime("%H:%M ") + why])[-5:]

    # ---------- Intear push stream ----------
    def ws_ok(self) -> bool:
        return time.time() - self.ws_last < 90

    async def follow_intear(self, bot):
        """Subscribe to new tokens on Intear; memepad ones are posted. Reconnects forever."""
        try:
            import websockets
        except ImportError:
            self.ws_state = "websockets library missing (pip install websockets)"
            log.error(self.ws_state)
            return
        await asyncio.sleep(3)
        while True:
            try:
                async with websockets.connect(INTEAR_WS, ping_interval=20, ping_timeout=20,
                                              max_size=16 * 1024 * 1024, open_timeout=15) as ws:
                    params = {"fields": ["symbol", "decimals", "created_at_millis"],
                              "where": {"created_after_millis": int((time.time() - 120) * 1000)}}
                    if self.ws_resume:
                        params["resume_from"] = self.ws_resume
                    await ws.send(json.dumps({"id": 1, "method": "tokens.subscribe", "params": params}))
                    self.ws_state = "connected"
                    log.info("Intear: connected, subscribed to new tokens")
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=60)  # server sends 'idle' every 15 s
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        self.ws_last = time.time()
                        if isinstance(msg.get("height"), int):
                            self.ws_resume = msg["height"]
                        if msg.get("error"):
                            self.ws_state = f"error: {str(msg['error'])[:120]}"
                            log.warning("Intear: %s", msg)
                        created = msg.get("created") or []
                        if not created and isinstance(msg.get("result"), dict):
                            created = msg["result"].get("created") or []
                        for acc in created:
                            fac = factory_of(acc) if isinstance(acc, str) else None
                            if fac:
                                self.ws_tokens += 1
                                asyncio.create_task(self._on_intear_token(bot, acc, fac, msg))
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                self.ws_state = f"reconnecting ({type(ex).__name__}: {str(ex)[:80]})"
                log.info("Intear: %s", self.ws_state)
                await asyncio.sleep(3)

    async def _find_dev(self, token: str, height) -> tuple[str, str]:
        """Signer of the deploy: look at the few blocks around the height Intear reported (≤5 requests)."""
        if not isinstance(height, int):
            return "", ""
        for h in (height, height - 1, height - 2, height + 1, height - 3):
            ok, blk = await self.block(h)
            for d in parse_block(blk or {}) if ok else []:
                if d["token"] == token:
                    dev = d["signer"]
                    if not dev or dev in FACTORIES:
                        dev = d["predecessor"] if d["predecessor"] not in FACTORIES else ""
                    return dev, d["tx"]
        return "", ""

    async def _on_intear_token(self, bot, token: str, fac: str, msg: dict):
        if self.store.q("SELECT 1 FROM deploys WHERE token=?", (token,)):
            return
        try:
            dev, tx = await asyncio.wait_for(self._find_dev(token, msg.get("height")), timeout=4)
        except asyncio.TimeoutError:
            dev, tx = "", ""
        if not dev:
            dev = await self.guess_dev(token)
        ts = (msg.get("timestamp_millis") or time.time() * 1000) / 1000
        await self.on_deploy(bot, {"token": token, "factory": fac, "signer": dev, "predecessor": "",
                                   "ts": ts, "tx": tx}, live=True)

    async def backfill(self, from_height: int):
        """Once: scan the last BACKFILL_DAYS days of blocks to learn older deploys (no posts)."""
        end = int(self.store.kv_get("deploy_backfill_cursor") or from_height)
        stop = from_height - int(BACKFILL_DAYS * 86400 / SECONDS_PER_BLOCK)
        log.info("Deploys backfill: blocks %d → %d", end, stop)
        found = 0
        h = end
        while h > stop:
            if self.head - self.cursor > 30:  # live feed is behind → let it catch up first
                await asyncio.sleep(3)
                continue
            heights = list(range(h, max(stop, h - 6), -1))
            results = await asyncio.gather(*(self.block(x) for x in heights))
            if not all(ok for ok, _ in results):
                await asyncio.sleep(5)
                continue
            for ok, blk in results:
                for d in parse_block(blk or {}):
                    if await self.on_deploy(None, d, live=False):
                        found += 1
            h = heights[-1] - 1
            if h % 300 < 6:
                self.store.kv_set("deploy_backfill_cursor", str(h))
            await asyncio.sleep(0.2)  # don't starve the live feed
        self.store.kv_set("deploy_backfill_done", "1")
        log.info("Deploys backfill done: %d deploys found", found)

    # ---------- deploy handling ----------
    async def on_deploy(self, bot, d: dict, live: bool) -> bool:
        dev = d["signer"]
        if not dev or dev in FACTORIES:
            dev = d["predecessor"] if d["predecessor"] not in FACTORIES else ""
        known = self.store.q("SELECT posted FROM deploys WHERE token=?", (d["token"],))
        if known:
            # backfill may have stored it silently before the live reader got there → still post it
            if live and bot and not known[0][0] and time.time() - d["ts"] < RETRY_WINDOW \
                    and d["token"] not in self.pending:
                self.pending.add(d["token"])
                asyncio.create_task(self._post_later(bot, d["token"]))
            return False
        self.store.q("INSERT OR IGNORE INTO deploys (token, dev, factory, ts) VALUES (?,?,?,?)",
                     (d["token"], dev, d["factory"], d["ts"]))
        log.info("Deploy: %s by %s", d["token"], dev)
        if live and bot:
            self.pending.add(d["token"])
            asyncio.create_task(self._post_later(bot, d["token"]))
        else:  # backfill: fill in name/ticker quietly
            meta = await self.view(d["token"], "ft_metadata")
            if isinstance(meta, dict):
                self.store.q("UPDATE deploys SET name=?, symbol=? WHERE token=?",
                             (str(meta.get("name") or "")[:60], str(meta.get("symbol") or "")[:20], d["token"]))
        return True

    async def _post_later(self, bot, token: str, delay: int | None = None):
        try:
            await asyncio.sleep(DEPLOY_DELAY if delay is None else delay)
            ch = self.channel()
            if not ch:
                self._miss(f"{token}: no channel set (/setdeploys)")
                return
            t0 = time.time()
            text, image, kb = await self.build(token, bot.username, fast=True)
            if text is None:
                self._miss(f"{token}: no ft_metadata yet, will retry")
                return
            msg = await self.send(bot, ch, text, image, kb)
            self.store.q("UPDATE deploys SET posted=1 WHERE token=?", (token,))
            log.info("Deploy posted: %s (built in %.1fs)", token, time.time() - t0)
            if (REFRESH_AFTER > 0 or LATE_REFRESH > 0) and msg:
                asyncio.create_task(self._refresh_later(bot, msg, token, text))
        except Exception as ex:
            log.exception("Deploy post %s failed", token)
            self._miss(f"{token}: post failed ({type(ex).__name__}: {str(ex)[:80]}), will retry")
        finally:
            self.pending.discard(token)

    async def _refresh_later(self, bot, msg, token: str, old_text: str):
        """Edit the same post later: dev's first buy / balance / ATH, and socials the dev filled on nearly after deploy."""
        waited = 0
        for at in (REFRESH_AFTER, LATE_REFRESH):
            if at <= waited:
                continue
            await asyncio.sleep(at - waited)
            waited = at
            try:
                if at == LATE_REFRESH:
                    self.scanner._nearly.pop(token, None)   # fresh nearly data, not the 30s cache
                text, _, kb = await self.build(token, bot.username, with_image=False)
                if not text or text == old_text:
                    continue
                old_text = text
                if msg.photo:
                    await bot.edit_message_caption(chat_id=msg.chat_id, message_id=msg.message_id, caption=text,
                                                   parse_mode=ParseMode.HTML, reply_markup=kb)
                else:
                    await bot.edit_message_text(chat_id=msg.chat_id, message_id=msg.message_id, text=text,
                                                parse_mode=ParseMode.HTML, reply_markup=kb,
                                                link_preview_options=NO_PREVIEW)
            except Exception as ex:
                log.info("Deploy refresh %s: %s", token, ex)

    async def retry_unposted(self, ctx=None):
        """Every minute: anything detected recently but not posted (RPC hiccup, Telegram error) → try again."""
        bot = getattr(ctx, "bot", None)
        if not bot or not self.channel():
            return
        rows = self.store.q("SELECT token FROM deploys WHERE posted=0 AND ts>? ORDER BY ts",
                            (time.time() - RETRY_WINDOW,))
        for (token,) in rows:
            if token not in self.pending:
                self.pending.add(token)
                asyncio.create_task(self._post_later(bot, token, delay=0))

    async def guess_dev(self, token: str) -> str:
        """For tokens deployed before tracking (/deploytest): ask the contract who owns it."""
        for m in ("get_owner", "owner", "owner_id", "get_creator", "creator", "get_owner_id"):
            v = await self.view(token, m)
            if isinstance(v, dict):
                v = v.get("owner_id") or v.get("creator") or v.get("owner")
            if isinstance(v, str) and v and v not in FACTORIES and not v.endswith(tuple(FACTORIES)):
                return v
        return ""

    async def token_ath(self, token: str) -> float:
        row = self.store.q("SELECT ath FROM deploys WHERE token=?", (token,))
        best = float(row[0][0] or 0) if row else 0.0
        try:
            c = await self.scanner.card(token, with_chart=True)
        except Exception:
            c = None
        if c:
            best = max(best, c.ath_mcap, c.fdv)
            if row:
                self.store.q("UPDATE deploys SET ath=? WHERE token=?", (best, token))
        return best

    async def _reference(self, meta: dict, fast: bool = False) -> dict:
        ref = meta.get("reference") or ""
        if ref.startswith("ipfs://"):
            ref = "https://ipfs.io/ipfs/" + ref[7:]
        if not ref.startswith("http"):
            return {}
        try:
            r = await self.http.get(ref, timeout=3 if fast else 8, follow_redirects=True)
            data = r.json()
            return data if isinstance(data, dict) else {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def _image(self, icon: str, fast: bool = False):
        if not icon:
            return None
        if icon.startswith("data:"):
            head, _, body = icon.partition(",")
            try:
                raw = base64.b64decode(body) if ";base64" in head else body.encode()
            except ValueError:
                return None
            return self._svg_to_png(raw) if "svg" in head else raw
        if icon.startswith("ipfs://"):
            icon = "https://ipfs.io/ipfs/" + icon[7:]
        if not icon.startswith("http"):
            return None
        try:
            r = await self.http.get(icon, timeout=4 if fast else 10, follow_redirects=True)
        except httpx.HTTPError:
            return None
        if r.status_code >= 400 or len(r.content) > 8_000_000:
            return None
        ctype = r.headers.get("content-type", "")
        if "svg" in ctype or r.content.lstrip()[:5] in (b"<?xml", b"<svg "):
            return self._svg_to_png(r.content)
        return r.content if ctype.startswith("image/") or r.content[:4] in (b"\x89PNG", b"RIFF", b"GIF8") \
            or r.content[:2] == b"\xff\xd8" else None

    @staticmethod
    def _svg_to_png(raw: bytes):
        try:
            import cairosvg
            return cairosvg.svg2png(bytestring=raw, output_width=512, output_height=512)
        except Exception:
            return None

    async def build(self, token: str, bot_username: str, dev: str | None = None,
                    fast: bool = False, with_image: bool = True):
        """(text, image bytes or None, keyboard) for one deploy.
        fast=True: for the first, instant post — no slow lookups (ATH from the bot's DB only)."""
        meta = None
        for i in range(30):  # metadata can appear a moment after the account: retry quickly, then slower
            meta = await self.view(token, "ft_metadata", finality="optimistic")
            if isinstance(meta, dict):
                break
            await asyncio.sleep(0.3 if i < 10 else 2)
        if not isinstance(meta, dict):
            log.info("Deploy %s: no ft_metadata, skipping", token)
            return None, None, None
        row = self.store.q("SELECT dev, factory FROM deploys WHERE token=?", (token,))
        fac = row[0][1] if row else (factory_of(token) or "")
        if dev is None:
            dev = (row[0][0] if row else "") or await self.guess_dev(token)
        name = str(meta.get("name") or "?")[:60]
        symbol = str(meta.get("symbol") or "?")[:20]
        decimals = int(meta.get("decimals") or 0)
        self.store.q("UPDATE deploys SET name=?, symbol=? WHERE token=?", (name, symbol, token))
        img_task = asyncio.create_task(self._image(str(meta.get("icon") or ""), fast)) if with_image else None
        supply_raw, extra = await asyncio.gather(self.view(token, "ft_total_supply", finality="optimistic"),
                                                 self._reference(meta, fast))
        try:
            supply_raw = int(supply_raw)
        except (TypeError, ValueError):
            supply_raw = 0
        desc = str(meta.get("description") or extra.get("description") or "").strip()
        socials = {k: extra.get(k) for k in ("website", "twitter", "telegram") if isinstance(extra.get(k), str)}
        if token.endswith(".nearlytrade.near"):
            # nearly.trade keeps X / Telegram / website in its own API, not in ft_metadata.reference
            try:
                nd = await self.scanner._nearly_launch(token)
            except Exception as ex:
                log.info("nearly links %s: %s", token, ex)
                nd = None
            nl = (nd or {}).get("links") or {}
            if isinstance(nl, dict):
                for k in ("website", "twitter", "telegram"):
                    v = nl.get(k) or (nl.get("x") if k == "twitter" else None)
                    if isinstance(v, str) and v.strip() and not socials.get(k):
                        socials[k] = v.strip()
        socials = {k: _norm_link(k, v) for k, v in socials.items()}

        label, page_tpl = FACTORIES.get(fac, ("NEAR", "https://nearblocks.io/token/{a}"))
        e = html.escape
        lines = [f"🚀 <b>{label} DEPLOY!</b>", "", "🏦 <b>Token Info</b>", f"<code>{e(token)}</code>"]
        info = [f"Name: <b>{e(name)}</b>", f"Ticker: <b>${e(symbol)}</b>"]
        if desc:
            info.append(f"Description: <i>{e(desc[:280])}{'…' if len(desc) > 280 else ''}</i>")
        if supply_raw:
            info.append(f"Supply: {fmt_amount(supply_raw / 10 ** decimals)}")
        links = []
        for k, icon in (("website", "🌐"), ("twitter", "𝕏"), ("telegram", "✈️")):
            v = socials.get(k, "")
            if v.startswith("http"):
                links.append(f'<a href="{e(v)}">{icon} {k.capitalize()}</a>')
        if links:
            info.append(" · ".join(links))
        lines += _tree(info)

        lines += ["", "💰 <b>Dev Info</b>"]
        if dev:
            lines.append(f'<a href="https://nearblocks.io/address/{e(dev)}"><code>{e(dev)}</code></a>')
            prev = self.store.q("SELECT token, symbol, factory FROM deploys WHERE dev=? AND token<>? "
                                "ORDER BY ts DESC", (dev, token))
            async def db_aths():
                return [float((self.store.q("SELECT ath FROM deploys WHERE token=?", (p[0],)) or [[0]])[0][0] or 0)
                        for p in prev[:6]]
            bal, held, aths = await asyncio.gather(
                self.near_balance(dev),
                self.view(token, "ft_balance_of", {"account_id": dev}, finality="optimistic"),
                db_aths() if fast else asyncio.gather(*(self.token_ath(p[0]) for p in prev[:6])))
            dev_rows = []
            if prev:
                names = []
                for t, sym, f in prev[:5]:
                    tpl = FACTORIES.get(f, (None, "https://nearblocks.io/token/{a}"))[1]
                    names.append(f'<a href="{e(tpl.format(a=t))}">{e(sym or t.split(".")[0].upper())}</a>')
                more = f" +{len(prev) - 5}" if len(prev) > 5 else ""
                dev_rows.append(f"🕑 Deploys: {len(prev)} ({', '.join(names)}{more})")
                best_i = max(range(len(aths)), key=lambda i: aths[i]) if aths else None
                if best_i is not None and aths[best_i] > 0:
                    t, sym, _ = prev[best_i]
                    dev_rows.append(f"🏆 Best ATH: <b>{usd(aths[best_i])}</b> ({e(sym or t.split('.')[0].upper())})")
                else:
                    dev_rows.append("🏆 Best ATH: —")
            else:
                dev_rows.append("🆕 First deploy from this wallet")
            if bal is not None:
                dev_rows.append(f"Ⓝ NEAR Balance: <b>{bal:,.2f} NEAR</b>")
            try:
                held = int(held)
            except (TypeError, ValueError):
                held = None
            if held is not None and supply_raw:
                dev_rows.append(f"🪙 Dev holds: <b>{held * 100 / supply_raw:.2f}%</b>")
            lines += _tree(dev_rows)
        else:
            lines.append("└ unknown")

        key = self.store.scan_key(token)
        kb_rows = [buy_buttons(token)]
        second = [InlineKeyboardButton({"NEARLY": "Nearly", "JUSTHOOT": "JustHoot"}.get(label, label.title()),
                                       url=page_tpl.format(a=token))]
        if bot_username:
            second.append(InlineKeyboardButton("🔎 Scan", url=f"https://t.me/{bot_username}?start=sc_{key}"))
        if dev:
            second.append(InlineKeyboardButton("👤 Dev", url=f"https://nearblocks.io/address/{dev}"))
        if second:
            kb_rows.append(second)

        text = "\n".join(lines)
        image = await img_task if img_task else None
        return text, image, InlineKeyboardMarkup(kb_rows)

    async def send(self, bot, chat_id, text: str, image, kb):
        kw = dict(chat_id=chat_id, parse_mode=ParseMode.HTML, reply_markup=kb)
        if image and _visible(text) <= 1024:
            try:
                return await bot.send_photo(photo=image, caption=text, **kw)
            except Exception as ex:
                log.info("Deploy photo failed (%s), sending text", ex)
        return await bot.send_message(text=text, link_preview_options=NO_PREVIEW, **kw)

    # ---------- ATH tracking ----------
    async def track_ath(self, _ctx=None):
        """Every few minutes: record the highest market cap of recently deployed tokens (for 'Best ATH')."""
        since = time.time() - ATH_TRACK_DAYS * 86400
        tokens = [r[0] for r in self.store.q("SELECT token FROM deploys WHERE ts>?", (since,))]
        for i in range(0, len(tokens), 30):
            try:
                r = await self.http.get(f"{DEXS}/tokens/v1/near/" + ",".join(tokens[i:i + 30]))
                data = r.json() if r.status_code < 400 else []
            except (httpx.HTTPError, ValueError):
                continue
            best: dict[str, float] = {}
            for p in data if isinstance(data, list) else []:
                a = (p.get("baseToken") or {}).get("address")
                try:
                    mc = float(p.get("marketCap") or p.get("fdv") or 0)
                except (TypeError, ValueError):
                    continue
                if a:
                    best[a] = max(best.get(a, 0), mc)
            for a, mc in best.items():
                self.store.q("UPDATE deploys SET ath=MAX(COALESCE(ath,0), ?) WHERE token=?", (mc, a))

    def status(self) -> str:
        n = self.store.q("SELECT COUNT(*), COUNT(DISTINCT dev) FROM deploys")[0]
        last = self.store.q("SELECT token, ts FROM deploys ORDER BY ts DESC LIMIT 1")
        lag = max(0, self.head - self.cursor) if self.head and self.cursor else None
        ch = self.channel()
        bf = "done" if self.store.kv_get("deploy_backfill_done") else (
            f"running ({BACKFILL_DAYS:g} days)" if BACKFILL_DAYS > 0 else "off")
        if self.source == "intear":
            age = f", last msg {int(time.time() - self.ws_last)}s ago" if self.ws_last else ""
            src = [f"🛰 Source: Intear stream — {html.escape(self.ws_state)}{age}; memepad tokens: {self.ws_tokens}",
                   "🧱 Block reader: " + ("sleeping (saves FastNear requests)" if self.ws_ok() or not FALLBACK
                                         else "ON as fallback while Intear is down")]
        else:
            src = ["🛰 Source: neardata (every block)"]
        out = [f"📡 Channel: <code>{html.escape(str(ch))}</code>" if ch else "📡 Channel: not set — /setdeploys @channel",
               f"🧱 Block: {self.cursor or '—'}" + (f" (behind by {lag})" if lag is not None else ""),
               f"⚡ Speed: {self.stats['blocks'] / max(1, time.time() - self.stats['t0']):.2f} blocks/s "
               f"(chain ≈1.6/s), workers {WORKERS}{', API key' if FASTNEAR_KEY else ''}"
               f"{', optimistic' if self.optimistic else ', final'} blocks",
               f"📨 neardata requests: {self.stats['requests'] * 3600 / max(1, time.time() - self.stats['t0']):,.0f}/hour "
               f"({self.stats['requests'] / max(1, self.stats['blocks']):.1f} per block)",
               f"❗ Fetch errors: {self.stats['errors']} (rate-limited {self.stats['limited']})"
               + (f", last: {html.escape(self.stats['last_err'])}" if self.stats['last_err'] else ""),
               f"🗂 Deploys stored: {n[0]} from {n[1]} devs",
               f"⏪ Backfill: {bf}"]
        out[1:1] = src
        if last:
            out.append(f"🆕 Last: <code>{html.escape(last[0][0])}</code> "
                       f"{int((time.time() - last[0][1]) // 60)} min ago")
        if self.misses:
            out.append("⚠️ Misses:\n" + "\n".join(html.escape(m) for m in self.misses))
        return "\n".join(out)
