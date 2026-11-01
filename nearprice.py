"""/near — live $NEAR price card with a candle chart (1D / 7D / 30D).

Stats: CoinGecko (price, market cap, rank, volume, 24h range, ATH, 1h/24h/7d change).
Candles: Kraken public OHLC (works from US servers, unlike Binance); fallback CoinGecko OHLC.
"""
from __future__ import annotations

import io
import logging
import time
from datetime import datetime, timezone

import httpx

log = logging.getLogger("nearprice")
CG = "https://api.coingecko.com/api/v3"
KRAKEN = "https://api.kraken.com/0/public/OHLC"
PERIODS = {"1d": (15, 96, 1), "7d": (60, 168, 7), "30d": (240, 180, 30)}  # kraken interval min, candles, cg days
_cache: dict[str, tuple[float, object]] = {}


async def _get(client, url, params=None, ttl=30):
    key = url + str(sorted((params or {}).items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        r = await client.get(url, params=params)
        if r.status_code >= 400:
            log.info("HTTP %s %s", r.status_code, url)
            return hit[1] if hit else None
        data = r.json()
        _cache[key] = (time.time(), data)
        return data
    except (httpx.HTTPError, ValueError) as e:
        log.info("request failed %s: %s", url, e)
        return hit[1] if hit else None


async def stats(client) -> dict | None:
    data = await _get(client, f"{CG}/coins/markets",
                      {"vs_currency": "usd", "ids": "near", "price_change_percentage": "1h,24h,7d"})
    return data[0] if isinstance(data, list) and data else None


async def candles(client, period: str) -> list[tuple]:
    interval, n, days = PERIODS[period]
    data = await _get(client, KRAKEN, {"pair": "NEARUSD", "interval": interval}, ttl=60)
    res = (data or {}).get("result") or {}
    rows = next((v for k, v in res.items() if k != "last"), None)
    if rows:
        return [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows[-n:]]
    data = await _get(client, f"{CG}/coins/near/ohlc", {"vs_currency": "usd", "days": days}, ttl=120)
    if isinstance(data, list):
        return [(r[0] / 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in data]
    return []


def big(x: float) -> str:
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.2f}{suf}"
    return f"${x:,.0f}"


def pct(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def caption(s: dict, period: str) -> str:
    ch24 = s.get("price_change_percentage_24h_in_currency") or s.get("price_change_percentage_24h") or 0
    dot = "🟢" if ch24 >= 0 else "🔴"
    ath = s.get("ath") or 0
    lines = [
        f"{dot} <b>$NEAR ${s.get('current_price', 0):,.3f}</b>  ({pct(ch24)} 24h)",
        "",
        f"⏱ 1H <b>{pct(s.get('price_change_percentage_1h_in_currency'))}</b> · "
        f"24H <b>{pct(ch24)}</b> · 7D <b>{pct(s.get('price_change_percentage_7d_in_currency'))}</b>",
        f"💰 MCap <b>{big(s.get('market_cap') or 0)}</b> · #{s.get('market_cap_rank') or '?'}",
        f"🔄 Vol 24h <b>{big(s.get('total_volume') or 0)}</b>",
        f"📏 24h range <b>${s.get('low_24h', 0):,.3f} – ${s.get('high_24h', 0):,.3f}</b>",
    ]
    if ath:
        lines.append(f"🏔 ATH <b>${ath:,.2f}</b> ({pct(s.get('ath_change_percentage'))})")
    lines.append(f"\n<i>Chart: {period.upper()} · CoinGecko / Kraken</i>")
    return "\n".join(lines)


def render(s: dict, rows: list[tuple], period: str) -> bytes | None:
    if len(rows) < 3:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bg, fg, dim, up, down, grid = "#0d1117", "#eef1f6", "#8b93a7", "#00ec97", "#ff5a67", "#1f2633"
    first, last = rows[0][1], rows[-1][4]
    ch = (last / first - 1) * 100 if first else 0
    col = up if ch >= 0 else down
    fig = plt.figure(figsize=(9, 5.2), dpi=130, facecolor=bg)
    head = fig.add_axes([0, 0.8, 1, 0.2], facecolor=bg)
    head.axis("off")
    head.set_xlim(0, 1)
    head.set_ylim(0, 1)
    head.text(0.04, 0.62, "NEAR", color=fg, fontsize=24, fontweight="bold", va="center")
    head.text(0.165, 0.62, "/ USD", color=dim, fontsize=14, va="center")
    head.text(0.04, 0.2, f"${(s.get('current_price') or last):,.3f}", color=col, fontsize=18, fontweight="bold", va="center")
    head.text(0.96, 0.62, f"{'▲' if ch >= 0 else '▼'} {abs(ch):.2f}%", color=col, fontsize=20, fontweight="bold",
              ha="right", va="center")
    head.text(0.96, 0.2, f"{period.upper()}  ·  MCap {big(s.get('market_cap') or 0)}  ·  #{s.get('market_cap_rank') or '?'}",
              color=dim, fontsize=11, ha="right", va="center")

    ax = fig.add_axes([0.04, 0.09, 0.86, 0.66], facecolor=bg)
    w = 0.65
    for i, (_, o, h, l, c) in enumerate(rows):
        cc = up if c >= o else down
        ax.plot([i, i], [l, h], color=cc, lw=0.9)
        ax.add_patch(Rectangle((i - w / 2, min(o, c)), w, max(abs(c - o), (h - l) * 0.02 or 1e-9), fc=cc, ec=cc))
    lo, hi = min(r[3] for r in rows), max(r[2] for r in rows)
    pad = (hi - lo) * 0.08 or hi * 0.02
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, len(rows))
    ax.axhline(last, color=col, ls=":", lw=0.9)
    ax.text(len(rows) + 0.4, last, f" {last:,.3f} ", color="#0d1117", fontsize=8, va="center", ha="left",
            bbox=dict(fc=col, ec="none", pad=1.5), clip_on=False)
    ax.yaxis.tick_right()
    ax.tick_params(colors=dim, labelsize=8, length=0)
    fmt = "%H:%M" if period == "1d" else "%d %b"
    step = max(1, len(rows) // 6)
    idx = list(range(0, len(rows), step))
    ax.set_xticks(idx)
    ax.set_xticklabels([datetime.fromtimestamp(rows[i][0], timezone.utc).strftime(fmt) for i in idx])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(color=grid, lw=0.6)
    fig.text(0.5, 0.42, "NEAR ANALYZER", color="white", alpha=0.04, fontsize=44, ha="center", va="center",
             fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
