"""Top lists: /top (memepads by 24h volume), /topmc (memepads by market cap), /topdex (trending on NEAR).

DexScreener has no public API for its "Hot" tab, so /topdex rebuilds it: candidates from GeckoTerminal
(trending + top-volume NEAR pools) + memepad and called tokens, data from DexScreener's public API, ranked by a
trending-like score (6h/1h volume, trades, buy pressure, price move, boosts). Majors, stables and bridged assets
(ETH, SOL, BTC, XRP, ZEC, USDC, wNEAR, *.omft.near …) are excluded — only native NEAR tokens.
"""
from __future__ import annotations

import html
import math
import re
import time

import httpx

GT = "https://api.geckoterminal.com/api/v2/networks/near/trending_pools"
GT_POOLS = "https://api.geckoterminal.com/api/v2/networks/near/pools"

# Not memes / not native NEAR: majors, stables, wrapped & bridged assets
MAJOR_SYMBOLS = {"NEAR", "WNEAR", "STNEAR", "LINEAR", "RNEAR", "XNEAR", "ETH", "WETH", "BTC", "WBTC", "NBTC", "SOL",
                 "XRP", "ZEC", "BNB", "TRX", "TON", "DOGE", "ADA", "AVAX", "SUI", "APT", "ARB", "OP", "LTC", "BCH",
                 "USDC", "USDT", "USDC.E", "USDT.E", "DAI", "FRAX", "USDE", "USN", "USD", "PYUSD", "AURORA", "WOO",
                 "LINK", "UNI", "AAVE", "PEPE", "SHIB", "TRUMP", "HYPE", "PAXG", "XAUT", "EURC"}
BRIDGED = re.compile(r"(\.omft\.near|\.omdep\.near|\.factory\.bridge\.near|\.bridge\.near|^aurora$|"
                     r"^wrap\.near$|tether-token\.near|\.sputnik-dao\.near|^[0-9a-f]{40,64}$|^0x)")


def is_meme(address: str, symbol: str) -> bool:
    a, s = (address or "").lower(), (symbol or "").upper()
    return bool(a) and not BRIDGED.search(a) and s not in MAJOR_SYMBOLS


async def gt_candidates(client: httpx.AsyncClient) -> list[str]:
    """Token addresses from GeckoTerminal trending + top-volume NEAR pools (2 requests, cached 2 min)."""
    hit = _cache.get("cand")
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    out: list[str] = []
    for url, params in ((GT, {"include": "base_token", "page": 1}),
                        (GT_POOLS, {"include": "base_token", "page": 1, "sort": "h24_volume_usd_desc"}),
                        (GT_POOLS, {"include": "base_token", "page": 2, "sort": "h24_volume_usd_desc"})):
        try:
            r = await client.get(url, params=params, headers={"Accept": "application/json;version=20230302"})
            payload = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            continue
        inc = {i["id"]: i.get("attributes", {}) for i in payload.get("included", []) or []}
        for d in payload.get("data") or []:
            base_id = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            b = inc.get(base_id, {})
            addr = b.get("address") or base_id.split("_", 1)[-1]
            if is_meme(addr, b.get("symbol")) and addr not in out:
                out.append(addr)
    if out:
        _cache["cand"] = (time.time(), out)
    return out or (hit[1] if hit else [])


def hot_score(p: dict) -> float:
    """DexScreener-like trending score from public pair data: recent volume, trades, price move, boosts."""
    vol6 = f((p.get("volume") or {}).get("h6"))
    vol1 = f((p.get("volume") or {}).get("h1"))
    tx6 = (p.get("txns") or {}).get("h6") or {}
    trades = f(tx6.get("buys")) + f(tx6.get("sells"))
    buys_ratio = f(tx6.get("buys")) / trades if trades else 0.5
    ch6 = max(-100.0, min(300.0, f((p.get("priceChange") or {}).get("h6"))))
    boosts = f((p.get("boosts") or {}).get("active"))
    return (1.0 * math.log10(1 + vol6) + 0.6 * math.log10(1 + vol1) + 1.1 * math.log10(1 + trades)
            + 0.8 * (buys_ratio - 0.5) + 0.004 * ch6 + 0.25 * math.log10(1 + boosts))


def hot_rows(pairs: list[dict], min_liq: float = 500, min_vol: float = 200) -> list[dict]:
    best: dict[str, dict] = {}
    for p in pairs:
        b = p.get("baseToken") or {}
        addr = b.get("address")
        if p.get("chainId") != "near" or not is_meme(addr, b.get("symbol")):
            continue
        if addr not in best or f((p.get("liquidity") or {}).get("usd")) > f((best[addr].get("liquidity") or {}).get("usd")):
            best[addr] = p
    rows = []
    for addr, p in best.items():
        liq, vol = f((p.get("liquidity") or {}).get("usd")), f((p.get("volume") or {}).get("h24"))
        if liq < min_liq or vol < min_vol:
            continue
        tx = (p.get("txns") or {}).get("h24") or {}
        b = p.get("baseToken") or {}
        rows.append(dict(address=addr, symbol=b.get("symbol") or "?", name=b.get("name") or "",
                         fdv=f(p.get("marketCap") or p.get("fdv")), vol=vol, liq=liq,
                         change=f((p.get("priceChange") or {}).get("h24")),
                         txns=int(f(tx.get("buys")) + f(tx.get("sells"))),
                         boosts=int(f((p.get("boosts") or {}).get("active"))), url=p.get("url") or "",
                         score=hot_score(p)))
    return sorted(rows, key=lambda r: r["score"], reverse=True)
_cache: dict[str, tuple[float, list]] = {}


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def usd(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.1f}{suf}"
    return f"${x:,.0f}"


async def trending(client: httpx.AsyncClient) -> list[dict]:
    hit = _cache.get("gt")
    if hit and time.time() - hit[0] < 120:
        return hit[1]
    rows = []
    try:
        r = await client.get(GT, params={"include": "base_token", "page": 1},
                             headers={"Accept": "application/json;version=20230302"})
        payload = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        payload = {}
    inc = {i["id"]: i.get("attributes", {}) for i in payload.get("included", []) or []}
    for d in payload.get("data") or []:
        a = d.get("attributes", {})
        base_id = (((d.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
        base = inc.get(base_id, {})
        rows.append(dict(address=base.get("address") or base_id.split("_", 1)[-1],
                         symbol=base.get("symbol") or (a.get("name") or "?").split("/")[0].strip(),
                         name=base.get("name") or "",
                         fdv=f(a.get("market_cap_usd") or a.get("fdv_usd")),
                         vol=f((a.get("volume_usd") or {}).get("h24")),
                         liq=f(a.get("reserve_in_usd")),
                         change=f((a.get("price_change_percentage") or {}).get("h24")),
                         pool=a.get("address", "")))
    seen, uniq = set(), []
    for r in rows:  # one line per token even if several of its pools trend
        if r["address"] not in seen and r["symbol"].upper() not in ("WNEAR", "NEAR", "USDC", "USDT"):
            seen.add(r["address"])
            uniq.append(r)
    if uniq:
        _cache["gt"] = (time.time(), uniq)
    return uniq or (hit[1] if hit else [])


TITLES = {"vol": "📊 <b>Top 10 memepads · 24h volume</b>",
          "mc": "💰 <b>Top 10 memepads · market cap</b>",
          "hot": "🔥 <b>Top 10 hot on NEAR</b>"}


def fmt(kind: str, rows: list[dict], link, pad_name) -> str:
    lines = [TITLES[kind], "<i>nearly + JustHoot</i>" if kind != "hot" else
             "<i>native NEAR tokens · DexScreener data, ranked by 6h activity</i>", ""]
    if not rows:
        lines.append("No data yet — try again in a minute.")
    medals = ["🥇", "🥈", "🥉"] + [f"{i}." for i in range(4, 11)]
    for i, r in enumerate(rows[:10]):
        ch = r["change"]
        arrow = "🟢" if ch >= 0 else "🔴"
        main = f"Vol <b>{usd(r['vol'])}</b> · MC {usd(r['fdv'])}" if kind == "vol" else \
            f"MC <b>{usd(r['fdv'])}</b> · Vol {usd(r['vol'])}" if kind == "mc" else \
            f"MC {usd(r['fdv'])} · Vol <b>{usd(r['vol'])}</b> · {r.get('txns', 0)} txns" + \
            (f" · ⚡{r['boosts']}" if r.get("boosts") else "")
        pad = pad_name(r["address"])
        href = (r.get("url") if kind == "hot" else None) or link(r)
        lines.append(f'{medals[i]} <a href="{html.escape(href, quote=True)}"><b>${html.escape(r["symbol"][:12])}</b></a>'
                     f' {arrow} {ch:+.0f}% 24h' + (f" · {pad}" if pad and kind == "hot" else ""))
        lines.append(f"     {main}")
    lines.append("\n<i>Tap a token to open it · send its contract to scan</i>")
    return "\n".join(lines)
