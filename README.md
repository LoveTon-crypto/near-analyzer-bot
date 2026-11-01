# NEAR Analyzer Bot

**An all-in-one Telegram analytics agent for NEAR memecoins** — token scanner, wallet PnL inspector, live memepad alerts, a new-deploys feed with dev history, and caller tracking with PnL cards.

**Try it live:** bot [@nearai_agent](https://t.me/nearai_agent) · utility chat with alerts [t.me/neardegen](https://t.me/neardegen) · new deploys channel [t.me/neardeploys](https://t.me/neardeploys) · 🇷🇺 [README на русском](README.ru.md)

Built for NEAR HACKATHON — tracks: *Memecoins With Mechanics*, *Agent Economy*, *Open Innovation*. The main product of a three-bot toolkit, see [Toolkit](#toolkit).

---

## Why

NEAR memecoins launch on memepads like **nearly.trade** and **JustHoot**, but traders have none of the tooling other chains have (TON / Solana scanner and tracker bots). Information is scattered across NearBlocks, DexScreener and the pads themselves. This bot puts it all in one place, inside Telegram where the community already lives.

## Features

| Feature | What it does | How to use |
|---|---|---|
| 🔎 **Token scanner** | Price, FDV, ATH, liquidity, volume, top-10 holders linked to their wallets, 15m candle chart | Paste a token contract or a nearly / JustHoot / DexScreener link, or `/scan <address>` |
| 👛 **Wallet inspector** | Trading PnL of any NEAR account: total and 7-day, realized + unrealized, per-token positions, shareable card | Paste a `.near` account in DM with the bot, or `/wallet <account>` |
| 🚀 **New deploys feed** | Detects every new token on nearly / JustHoot in real time and posts to a channel: avatar, name, ticker, description, dev wallet, dev NEAR balance, dev holdings, dev's previous deploys and their best ATH | `/setdeploys @channel` |
| 📈 **Group alerts** (forum topics) | DEX Screener *DEX Paid* / boosts / CTO / ads; memepad tokens crossing **$10K / $20K / $30K** market cap; **dips** of −15% or more | `/settopic dexpaid` … `/settopic dip` |
| 🎯 **Caller tracking** | First member to drop a contract in the group gets the call; the bot tracks MC at call time vs now and ATH since. **📊 PnL** button renders an image card with random degen characters (win / loss) | automatic · `/callers` leaderboard (24H / 7D / 30D / ALL) |
| 🏆 **Tops** | Top-10 memepad tokens by 24h volume, by market cap, and trending native NEAR tokens on DexScreener | `/top`, `/topmc`, `/topdex` |
| 📰 **Daily digest** | Every day: top-5 gainers and top-3 losers with an image table | `/settopic daily`, `/digest` |
| Ⓝ **NEAR price** | $NEAR price with 1D / 7D / 30D candle chart | `/near` |
| 🐦 **X watcher** (optional) | Reposts tweets from chosen accounts into a topic | `/xadd <handle>` (needs twitterapi.io key) |

Every alert has **🔎 Scan** and **🟢 Buy on Bettear** buttons, so a user goes from signal to analysis to trade in two taps.

## How it works

```
NEAR blockchain ─┬─ Intear stream / neardata blocks ──► deploys.py   (new tokens, dev history)
                 └─ NEAR RPC (FastNear)              ──► scanner.py   (holders, metadata, balances)
DexScreener / GeckoTerminal ─────────────────────────► scanner.py, bot.py (prices, charts, DEX Paid)
nearly.trade / JustHoot APIs ────────────────────────► bot.py       (memepad MC + dip alerts)
Intear API (positions, trades) ──────────────────────► wallet.py    (wallet PnL)
                                                          │
                              SQLite (calls, deploys, settings) ◄──┤
                                                          ▼
                                         Telegram (python-telegram-bot)
```

| File | Role |
|---|---|
| `bot.py` | Entry point: Telegram handlers, alert watchers, scheduling |
| `scanner.py` | Token card: market data, holders, chart |
| `wallet.py` | Wallet PnL inspector |
| `deploys.py` | Real-time new-token feed and dev history |
| `calls.py` | Caller tracking and leaderboard |
| `pnl_art.py`, `characters.py` | PnL card rendering with generated characters |
| `tops.py`, `digest.py` | Top lists and the daily digest |
| `nearprice.py` | $NEAR price and chart |
| `xwatch.py` | Optional X/Twitter watcher |

All data sources are free public APIs — no paid keys are required for the core features.

## Run it yourself

Requirements: Python 3.10+, a bot token from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/LoveTon-crypto/near-analyzer-bot.git
cd near-analyzer-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env        # then put your BOT_TOKEN in .env
venv/bin/python bot.py
```

Then open your bot in Telegram, send `/start` (the first user to do so becomes the owner/admin), and paste any NEAR token contract — e.g. `batman-4.nearlytrade.near` — to get a scan.

`cairosvg` needs the system Cairo library for SVG token icons (`apt install libcairo2` on Debian/Ubuntu).

### Production (Ubuntu VPS, systemd)

```bash
cd /root && git clone https://github.com/LoveTon-crypto/near-analyzer-bot.git
cd near-analyzer-bot && printf 'BOT_TOKEN=your_token\n' > .env
bash deploy.sh             # creates venv, installs a systemd service, starts the bot
```

Updates afterwards: `analyzer-update`.

### Group setup

1. Create a Telegram group, enable **Topics**, add the bot as admin.
2. In each topic send the matching command:

| Topic | Command |
|---|---|
| DEX Paid | `/settopic dexpaid` |
| Boosts | `/settopic boost` |
| CTO | `/settopic cto` |
| Ads | `/settopic ads` |
| MC $10K / $20K / $30K | `/settopic 10k` · `20k` · `30k` |
| Dips | `/settopic dip` |
| Daily digest | `/settopic daily` |
| X posts | `/settopic x` |

`/topics` lists bindings, `/test dexpaid` sends a sample alert, `/admin` shows owner settings.

### Configuration

All settings live in `.env` (see [`.env.example`](.env.example)): poll interval, MC levels (`MCAP_LEVELS`), dip threshold (`DIP_PCT`), digest time and timezone, deploys channel, referral code, optional FastNear / twitterapi.io keys.

## Toolkit

Three open-source Telegram tools for NEAR memecoin traders, each in its own repo:

| Tool | Live | Repo |
|---|---|---|
| **NEAR Analyzer** — token scanner, wallet PnL, memepad alerts, new deploys feed, caller PnL | [@nearai_agent](https://t.me/nearai_agent) · [chat](https://t.me/neardegen) · [deploys](https://t.me/neardeploys) | this repo |
| **NEAR DEX Alerts** — volume spikes, pumps, dumps and pump & dumps across NEAR DEXes | [@nearaidex_bot](https://t.me/nearaidex_bot) | [near-dex-bot](https://github.com/LoveTon-crypto/near-dex-bot) |
| **NEAR Wallet Tracker** — real-time activity of named wallets | [@nearwallettrack_bot](https://t.me/nearwallettrack_bot) | [near-wallet-tracker](https://github.com/LoveTon-crypto/near-wallet-tracker) |

The analyzer answers *"what is this token / wallet?"*, the DEX bot answers *"what is moving right now?"*, and the tracker answers *"what are the wallets I follow doing?"*.

## Roadmap

- On-chain bonding progress once memepads expose curve data
- Copy-trade and sniper actions directly from alerts
- Smart-money wallet labels from caller and wallet PnL history
