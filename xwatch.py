"""X (Twitter) posts → Telegram topic.

Uses twitterapi.io (official X API has no free reads in 2026). One search request per poll covers all
watched accounts: "(from:a OR from:b) since_time:…" — so cost stays ~$3/month at a 2-minute interval.
Set TWITTERAPI_KEY in .env. Accounts are managed by the owner: /xadd, /xdel, /xlist.
"""
from __future__ import annotations

import html
import logging
import os
import time
from email.utils import parsedate_to_datetime

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

log = logging.getLogger("xwatch")
API = "https://api.twitterapi.io/twitter/tweet/advanced_search"
KEY = os.environ.get("TWITTERAPI_KEY", "")
POLL_SECONDS = int(os.environ.get("X_POLL_SECONDS", "120"))
INCLUDE_REPLIES = os.environ.get("X_INCLUDE_REPLIES", "0") == "1"
INCLUDE_RETWEETS = os.environ.get("X_INCLUDE_RETWEETS", "1") == "1"


def setup(db):
    db.execute("CREATE TABLE IF NOT EXISTS x_accounts (username TEXT PRIMARY KEY, added REAL)")
    db.commit()


def accounts(db) -> list[str]:
    return [r[0] for r in db.execute("SELECT username FROM x_accounts ORDER BY username").fetchall()]


def clean(name: str) -> str:
    name = name.strip().lstrip("@")
    if "x.com/" in name or "twitter.com/" in name:
        name = name.split(".com/", 1)[1].split("/")[0].split("?")[0]
    return "".join(ch for ch in name if ch.isalnum() or ch == "_")[:15]


def add(db, name: str) -> str | None:
    u = clean(name)
    if not u:
        return None
    db.execute("INSERT OR IGNORE INTO x_accounts VALUES (?,?)", (u, time.time()))
    db.commit()
    return u


def remove(db, name: str) -> bool:
    u = clean(name)
    cur = db.execute("DELETE FROM x_accounts WHERE lower(username)=lower(?)", (u,))
    db.commit()
    return cur.rowcount > 0


async def fetch(client: httpx.AsyncClient, users: list[str], since: int) -> list[dict]:
    q = "(" + " OR ".join(f"from:{u}" for u in users) + f") since_time:{since}"
    if not INCLUDE_REPLIES:
        q += " -filter:replies"
    if not INCLUDE_RETWEETS:
        q += " -filter:retweets"
    try:
        r = await client.get(API, params={"query": q, "queryType": "Latest"}, headers={"X-API-Key": KEY})
        if r.status_code >= 400:
            log.warning("twitterapi.io HTTP %s: %s", r.status_code, r.text[:200])
            return []
        return (r.json() or {}).get("tweets") or []
    except (httpx.HTTPError, ValueError) as e:
        log.warning("twitterapi.io failed: %s", e)
        return []


def _ts(tw: dict) -> float:
    try:
        return parsedate_to_datetime(tw.get("createdAt") or "").timestamp()
    except (TypeError, ValueError):
        return 0.0


def first_photo(tw: dict) -> str | None:
    for src in (tw, tw.get("retweeted_tweet") or {}, tw.get("quoted_tweet") or {}):
        for m in ((src.get("extendedEntities") or {}).get("media") or []):
            if m.get("media_url_https") and m.get("type") in ("photo", "video", "animated_gif", None):
                return m["media_url_https"]
    return None


def fmt(tw: dict) -> tuple[str, str | None, str]:
    """Returns (html text, photo url, link)."""
    a = tw.get("author") or {}
    who = f"<b>{html.escape(a.get('name') or a.get('userName') or '?')}</b> (@{html.escape(a.get('userName') or '?')})"
    link = tw.get("url") or f"https://x.com/{a.get('userName')}/status/{tw.get('id')}"
    rt = tw.get("retweeted_tweet")
    if rt:
        ra = rt.get("author") or {}
        head = f"🔁 {who} reposted @{html.escape(ra.get('userName') or '?')}"
        body = rt.get("text") or ""
    else:
        kind = "replied" if tw.get("isReply") else ("quoted" if tw.get("quoted_tweet") else "posted")
        head = f"𝕏 {who} {kind}"
        body = tw.get("text") or ""
    lines = [head, "", html.escape(body.strip())[:3000]]
    qt = tw.get("quoted_tweet")
    if qt and not rt:
        qa = (qt.get("author") or {}).get("userName") or "?"
        lines += ["", f"<blockquote>@{html.escape(qa)}: {html.escape((qt.get('text') or '').strip())[:600]}</blockquote>"]
    stats = [f"❤️ {tw['likeCount']}" if tw.get("likeCount") else "", f"🔁 {tw['retweetCount']}" if tw.get("retweetCount") else ""]
    stats = " · ".join(s for s in stats if s)
    if stats:
        lines += ["", stats]
    return "\n".join(lines), first_photo(tw), link


class XWatcher:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=25)

    async def poll(self, db, seen, mark, kv_get, kv_set) -> list[dict]:
        """New tweets from watched accounts, oldest first. First run only remembers the current moment."""
        users = accounts(db)
        if not KEY or not users:
            return []
        now = int(time.time())
        since = int(kv_get("x_since") or 0)
        kv_set("x_since", str(now - 120))   # small overlap; duplicates are filtered by id
        if not since:
            return []
        out = []
        for i in range(0, len(users), 12):   # keep the query short
            for tw in await self.fetch_batch(users[i:i + 12], since):
                key = f"x:{tw.get('id')}"
                if tw.get("id") and not seen(key):
                    mark(key)
                    out.append(tw)
        return sorted(out, key=_ts)

    async def fetch_batch(self, users, since):
        return await fetch(self.client, users, since)

    async def close(self):
        await self.client.aclose()
