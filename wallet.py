"""Wallet PnL ("Inspect Wallet") for any NEAR account.

Data: Intear API (https://api.intea.rs, free during beta, no key):
  • /positions — every token position built from the wallet's swaps: bought/sold USD, realized PnL, amount left
  • /trades    — swaps of the last 7 days (for the 7d block)
  • /balances  — tokens held now + prices (to value what's still held)
NEAR balance comes from public NEAR RPC (no FastNear key used).
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import time

import httpx

log = logging.getLogger("wallet")

INTEAR = "https://api.intea.rs"
DEXS = "https://api.dexscreener.com"
RPC_URLS = ["https://rpc.mainnet.fastnear.com", "https://free.rpc.fastnear.com", "https://rpc.mainnet.near.org"]
# quote tokens — not "positions" in the memecoin sense
BASES = {"wrap.near", "near", "usdt.tether-token.near",
         "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",  # USDC
         "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.factory.bridge.near",       # USDC.e
         "dac17f958d2ee523a2206206994597c13d831ec7.factory.bridge.near"}       # USDT.e


def _table(data) -> list[dict]:
    """Intear answers {result: {columns, rows}} (or the inner object directly); rows may be lists or dicts."""
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if not isinstance(data, dict):
        return []
    rows = data.get("rows") or data.get("positions") or data.get("trades") or []
    cols = data.get("columns") or []
    out = []
    for r in rows:
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, list) and cols:
            out.append(dict(zip(cols, r)))
    return out


def token_url(t: str) -> str:
    if t.endswith(".nearlytrade.near"):
        return f"https://nearly.trade/{t}"
    if t.endswith("launchpad.justhoot.near"):
        return f"https://justhoot.fun/p/{t}"
    return f"https://nearblocks.io/token/{t}"


def f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def money(x: float) -> str:
    sign = "-" if x < 0 else ""
    x = abs(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{sign}${x / div:.2f}".rstrip("0").rstrip(".") + suf
    return f"{sign}${x:,.2f}"


def signed(x: float) -> str:
    return ("+" if x >= 0 else "") + money(x)


def nears(x_usd: float, near_usd: float) -> str:
    if near_usd <= 0:
        return ""
    v = x_usd / near_usd
    return f" <i>≈{v:,.0f} Ⓝ</i>" if abs(v) >= 100 else f" <i>≈{v:,.1f} Ⓝ</i>"


class WalletPnL:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self._sym: dict[str, str] = {}
        self._dec: dict[str, int] = {}
        self._cache: dict[str, tuple[float, dict]] = {}

    async def _get(self, path: str, params: dict, timeout: float = 25):
        try:
            r = await self.http.get(f"{INTEAR}{path}", params=params, timeout=timeout)
        except httpx.HTTPError as e:
            log.info("intear %s: %s", path, e)
            return None
        if r.status_code >= 400:
            log.info("intear %s -> HTTP %s %s", path, r.status_code, r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            return None

    async def _rpc(self, params: dict):
        payload = {"jsonrpc": "2.0", "id": "1", "method": "query", "params": params}
        for url in RPC_URLS:
            try:
                data = (await self.http.post(url, json=payload, timeout=10)).json()
            except (httpx.HTTPError, ValueError):
                continue
            if "result" in data:
                return data["result"]
            if "UNKNOWN_ACCOUNT" in json.dumps(data.get("error") or ""):
                return None
        return None

    async def account(self, acc: str):
        res = await self._rpc({"request_type": "view_account", "finality": "final", "account_id": acc})
        return res if isinstance(res, dict) and "amount" in res else None

    async def symbol(self, token: str) -> str:
        if token in self._sym:
            return self._sym[token]
        res = await self._rpc({"request_type": "call_function", "finality": "final", "account_id": token,
                               "method_name": "ft_metadata", "args_base64": base64.b64encode(b"{}").decode()})
        sym = ""
        try:
            sym = str(json.loads(bytes(res["result"]).decode()).get("symbol") or "")[:16]
        except (TypeError, KeyError, ValueError, AttributeError):
            pass
        sym = sym or token.split(".")[0].upper()[:16]
        self._sym[token] = sym
        return sym

    async def positions(self, acc: str) -> list[dict] | None:
        data = await self._get("/positions", {"account": acc, "limit": 1000})
        return None if data is None else _table(data)

    async def _view(self, account: str, method: str, args: dict | None = None):
        res = await self._rpc({"request_type": "call_function", "finality": "final", "account_id": account,
                               "method_name": method,
                               "args_base64": base64.b64encode(json.dumps(args or {}).encode()).decode()})
        try:
            return json.loads(bytes(res["result"]).decode())
        except (TypeError, KeyError, ValueError, AttributeError):
            return None

    async def _decimals(self, token: str) -> int | None:
        if token in self._dec:
            return self._dec[token]
        meta = await self._view(token, "ft_metadata")
        if not isinstance(meta, dict):
            return None
        self._dec[token] = int(f(meta.get("decimals")))
        if meta.get("symbol"):
            self._sym.setdefault(token, str(meta["symbol"])[:16])
        return self._dec[token]

    async def _rpc_amount(self, acc: str, token: str) -> float:
        raw, dec = await asyncio.gather(self._view(token, "ft_balance_of", {"account_id": acc}),
                                        self._decimals(token))
        return f(raw) / 10 ** dec if dec is not None else 0.0

    async def dex_prices(self, tokens: list[str]) -> dict[str, float]:
        """USD price from DexScreener (the pair with the most liquidity), 30 tokens per request."""
        out: dict[str, tuple[float, float]] = {}
        for i in range(0, len(tokens), 30):
            try:
                r = await self.http.get(f"{DEXS}/tokens/v1/near/" + ",".join(tokens[i:i + 30]), timeout=15)
                data = r.json() if r.status_code < 400 else []
            except (httpx.HTTPError, ValueError):
                continue
            for p in data if isinstance(data, list) else []:
                a = (p.get("baseToken") or {}).get("address")
                liq = f((p.get("liquidity") or {}).get("usd"))
                if a and f(p.get("priceUsd")) > 0 and liq >= out.get(a, (0, -1))[1]:
                    out[a] = (f(p.get("priceUsd")), liq)
        return {a: v[0] for a, v in out.items()}

    async def holdings(self, acc: str, extra_tokens: set[str]) -> tuple[dict[str, float], dict[str, float]]:
        """token -> USD value held now. Amounts: Intear balances, checked on-chain for tokens Intear
        doesn't price/list; prices: Intear, else DexScreener."""
        wnear = {"direct": 0.0, "rhea": 0.0}
        data = await self._get("/balances", {"account": acc, "sources": "direct,rhea",
                                             "token_fields": "symbol,decimals,price_usd"})
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        amounts: dict[str, float] = {}
        prices: dict[str, float] = {}
        if isinstance(data, dict):
            toks = data.get("tokens") or {}
            for r in _table(data.get("balances") or []):
                t = r.get("token")
                if not t:
                    continue
                meta = toks.get(t) or {}
                if meta.get("symbol"):
                    self._sym[t] = str(meta["symbol"])[:16]
                if meta.get("decimals") not in (None, ""):
                    self._dec[t] = int(f(meta["decimals"]))
                    amt = f(r.get("balance")) / 10 ** self._dec[t]
                    amounts[t] = amounts.get(t, 0.0) + amt
                    if t == "wrap.near":
                        src = "rhea" if str(r.get("source")) == "rhea" else "direct"
                        wnear[src] += amt
                if f(meta.get("price_usd")) > 0:
                    prices[t] = f(meta["price_usd"])
        # wNEAR (trading bots keep their NEAR as wNEAR): always checked on-chain if Intear didn't list it
        if "wrap.near" not in amounts:
            w = await self._rpc_amount(acc, "wrap.near")
            if w > 0:
                amounts["wrap.near"] = w
                wnear["direct"] = w
        # tokens from the trade history that Intear didn't report: read the balance on-chain
        missing = [t for t in extra_tokens if t not in amounts][:60]
        if missing:
            vals = await asyncio.gather(*(self._rpc_amount(acc, t) for t in missing))
            amounts.update({t: v for t, v in zip(missing, vals) if v > 0})
        unpriced = [t for t, a in amounts.items() if a > 0 and t not in prices]
        if unpriced:
            prices.update(await self.dex_prices(unpriced))
        return {t: a * prices.get(t, 0.0) for t, a in amounts.items()}, wnear

    async def trades_since(self, acc: str, days: int) -> tuple[list[dict], bool]:
        """Swaps of the last `days` days, newest first. (rows, truncated?)"""
        since = int((time.time() - days * 86400) * 1000)
        rows, cursor = [], None
        for _ in range(15):  # up to 3000 swaps
            params = {"trader": acc, "after_millis": since, "limit": 200, "order": "desc",
                      "fields": "id,side,token,usd_value,timestamp_millis"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/trades", params)
            if data is None:
                break
            inner = data.get("result", data) if isinstance(data, dict) else {}
            rows += _table(data)
            cursor = inner.get("next_cursor") if isinstance(inner, dict) else None
            if not cursor:
                return rows, False
        return rows, bool(cursor)

    # ------------------------------------------------------------------
    async def load(self, acc: str) -> dict | None:
        """Everything about the wallet, fetched once and cached for 2 minutes (period buttons reuse it)."""
        hit = self._cache.get(acc)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
        info = await self.account(acc)
        if info is None:
            return None
        pos, (tr, truncated) = await asyncio.gather(self.positions(acc), self.trades_since(acc, 30))
        open_tokens = {p["token"] for p in (pos or [])
                       if p.get("token") and (p.get("closed_at_millis") is None or f(p.get("amount")) > 0)}
        hv, wnear = await self.holdings(acc, open_tokens - BASES)
        d = {"acc": acc, "near": f(info.get("amount")) / 1e24, "wnear": wnear, "hold": hv, "pos": pos,
             "trades": tr, "truncated": truncated}
        self._cache[acc] = (time.time(), d)
        if len(self._cache) > 200:
            self._cache.pop(next(iter(self._cache)))
        return d

    @staticmethod
    def stats(d: dict, period: str) -> dict | None:
        """period: '7d' | '30d' | 'all'. Per-token rows + totals."""
        hold = d["hold"]
        per: dict[str, dict] = {}
        points: list[tuple[float, float]] = []
        if period == "all":
            if d["pos"] is None:
                return None
            for p in d["pos"]:
                t = p.get("token")
                if not t or t in BASES:
                    continue
                m = per.setdefault(t, {"b": 0.0, "s": 0.0, "real": 0.0, "buys": 0, "sells": 0})
                m["b"] += f(p.get("bought_usd"))
                m["s"] += f(p.get("sold_usd"))
                m["real"] += f(p.get("realized_pnl_usd"))
                m["buys"] += int(f(p.get("buys")))
                m["sells"] += int(f(p.get("sells")))
            for t, m in per.items():
                m["h"] = hold.get(t, 0.0)
        else:
            since = (time.time() - (7 if period == "7d" else 30) * 86400) * 1000
            rows = sorted((r for r in d["trades"] if f(r.get("timestamp_millis")) >= since),
                          key=lambda r: f(r.get("timestamp_millis")))
            cum = 0.0
            for r in rows:
                t = r.get("token")
                side = str(r.get("side")).lower()
                if not t or t in BASES or side not in ("buy", "sell"):
                    continue
                v = f(r.get("usd_value"))
                m = per.setdefault(t, {"b": 0.0, "s": 0.0, "real": 0.0, "buys": 0, "sells": 0})
                if side == "buy":
                    m["b"] += v
                    m["buys"] += 1
                    cum -= v
                else:
                    m["s"] += v
                    m["sells"] += 1
                    cum += v
                points.append((f(r.get("timestamp_millis")), cum))
            for t, m in per.items():  # what's still held counts only for tokens bought in this period
                m["h"] = hold.get(t, 0.0) if m["buys"] else 0.0
        for m in per.values():
            m["pnl"] = m["s"] + m["h"] - m["b"]
        B = sum(m["b"] for m in per.values())
        S = sum(m["s"] for m in per.values())
        H = sum(m["h"] for m in per.values())
        P = S + H - B
        if points:
            points.append((time.time() * 1000, points[-1][1] + H))
        traded = {t: m for t, m in per.items() if m["b"] > 0}
        ranked = sorted(traded.items(), key=lambda kv: kv[1]["pnl"], reverse=True)
        return {"period": period, "B": B, "S": S, "H": H, "P": P, "pct": P / B * 100 if B > 0 else 0.0,
                "real": sum(m["real"] for m in per.values()) if period == "all" else None,
                "wins": sum(1 for m in traded.values() if m["pnl"] > 0), "n": len(traded),
                "buys": sum(m["buys"] for m in per.values()), "sells": sum(m["sells"] for m in per.values()),
                "best": [kv for kv in ranked if kv[1]["pnl"] > 0][:3],
                "worst": [kv for kv in reversed(ranked) if kv[1]["pnl"] < 0][:3],
                "points": [p[1] for p in points], "empty": not per}

    async def labels(self, tokens: list[str]) -> dict[str, str]:
        """Ticker per token; same ticker for different contracts → add the contract name."""
        syms = await asyncio.gather(*(self.symbol(t) for t in tokens))
        clean = lambda x: "".join(ch for ch in str(x) if ch.isprintable() and not ch.isspace()) or "?"
        sm = {t: clean(v) for t, v in zip(tokens, syms)}
        keys = [v.upper() for v in sm.values()]
        return {t: v + ("·" + t.split(".")[0][:12] if keys.count(v.upper()) > 1 else "") for t, v in sm.items()}

    async def report(self, acc: str, near_usd: float, period: str = "all"):
        """(HTML text, stats) or (None, None) if the account doesn't exist."""
        d = await self.load(acc)
        if d is None:
            return None, None
        e = html.escape
        near_bal, wn, wr = d["near"], d["wnear"]["direct"], d["wnear"]["rhea"]
        total_near = near_bal + wn + wr
        parts = [f"{near_bal:,.2f} native"] + ([f"{wn:,.2f} wNEAR"] if wn >= 0.01 else []) \
            + ([f"{wr:,.2f} in Rhea"] if wr >= 0.01 else [])
        held_value = sum(v for t, v in d["hold"].items() if t not in BASES)
        lines = ["🔎 <b>Inspect Wallet</b>", "",
                 f'👛 Wallet: <a href="https://nearblocks.io/address/{e(acc)}"><code>{e(acc)}</code></a>',
                 f"Ⓝ NEAR: <b>{total_near:,.2f}</b>" + (f" ≈ {money(total_near * near_usd)}" if near_usd else "")
                 + (f" <i>({', '.join(parts)})</i>" if len(parts) > 1 else ""),
                 f"💰 Tokens value: <b>{money(held_value)}</b>", ""]
        st = self.stats(d, period)
        title = {"all": "🚀 <b>Total PnL</b>", "30d": "📅 <b>30d PnL</b>", "7d": "⚡ <b>7d PnL</b>"}[period]
        if st is None:
            lines += [title, "⚠️ Trade history service is not answering right now, try again in a minute."]
            return "\n".join(lines), None
        if st["empty"]:
            lines += [title + (": no swaps found" if period == "all" else ": no swaps in this period")]
            return "\n".join(lines), st
        B, S, H, P = st["B"], st["S"], st["H"], st["P"]
        block = (f"📥 Bought: <b>{money(B)}</b>{nears(B, near_usd)}\n"
                 f"📦 Hold: <b>{money(H)}</b>{nears(H, near_usd)}\n"
                 f"✅ Sold: <b>{money(S)}</b>{nears(S, near_usd)}\n"
                 f"{'🟢' if P >= 0 else '🔴'} PnL: <b>{signed(P)}</b>" + (f" <b>{st['pct']:+.0f}%</b>" if B > 0 else "") + "\n")
        if st["real"] is not None:
            block += f"💵 Realized: {signed(st['real'])} · Unrealized: {signed(P - st['real'])}\n"
        block += (f"🎯 Win rate: <b>{st['wins']}/{st['n']}</b> tokens"
                  + (f" ({st['wins'] / st['n'] * 100:.0f}%)" if st["n"] else "")
                  + f" · {st['buys']} buys / {st['sells']} sells")
        lines += [title, f"<blockquote>{block}</blockquote>"]
        lab = await self.labels([t for t, _ in st["best"] + st["worst"]])

        def item(kv) -> str:
            t, m = kv
            return (f'<a href="{e(token_url(t))}">${e(lab[t])}</a> {signed(m["pnl"])}'
                    + (f" ({m['pnl'] / m['b'] * 100:+.0f}%)" if m["b"] else ""))

        if st["best"]:
            lines.append("🏆 Best: " + " · ".join(item(kv) for kv in st["best"]))
        if st["worst"]:
            lines.append("💀 Worst: " + " · ".join(item(kv) for kv in st["worst"]))
        note = "USD at trade time · Hold = current value"
        if period != "all" and d["truncated"]:
            note += " · only the latest 3000 swaps"
        lines.append(f"<i>{note}</i>")
        st["best_label"] = lab[st["best"][0][0]] if st["best"] else ""
        return "\n".join(lines), st
