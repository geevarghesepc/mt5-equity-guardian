# MT5 Stoploss Guardian

An automated guardian script for MetaTrader 5, designed to enforce a strict two-position stop-loss policy, prevent averaging down, and implement a daily circuit breaker. Built for discretionary manual scalpers.

**⚠️ Requires a hedging-mode MT5 account.** Non-hedging (netting) accounts cannot manage separate same-symbol tickets and will be blocked in live mode.

## Features

- **Two-Position Maximum**: At most 2 managed positions at any time. When a third appears, the oldest ticket (by open time) is closed.
- **Independent 5% Per-Ticket SL**: Each ticket gets its own `balance × 5%` broker stop-loss. No volume splitting, no shared budget. Two tickets = 10% maximum aggregate risk.
- **Same-Symbol Admission**: When a new ticket is detected on a symbol that already has a managed ticket:
  - If the older ticket's floating profit is **≤ 0** → close the older ticket immediately.
  - If the older ticket's floating profit is **> 0** (pyramiding a winner) → keep it.
  - The admission decision is evaluated when a new ticket is detected. An accepted ticket is not closed merely because its profit turns negative while holding; however, a previously accepted ticket is re-evaluated on each later add on that symbol.
- **Startup Baseline**: Positions already open when the guardian starts are subject to the global cap. Baseline tickets are only exempt from comparison against each other at startup; they are evaluated if a new same-symbol ticket is later opened.
- **Scaled Trailing Equity Guard**: The trailing stop threshold is `peak_equity − balance × 5% × min(position_count, 2)`. With two tickets the combined allowance is 10%.
- **2→1 Peak Reset**: When position count drops from 2 to 1 (including after a policy close), peak equity is reset to current equity before the new 5% trailing threshold takes effect.
- **SL Preservation**: Tighter manual/existing SLs are never widened. Missing SLs are set; looser SLs are tightened. Tickets where the broker minimum stop distance exceeds the 5% budget or contract specs are invalid are reported as unprotected and closed. Transient broker SL placement failures wait `close_retries` consecutive loops before emergency closing naked positions, with failed broker close requests throttled via `emergency_close_retry_interval` (5s default).
- **Daily Circuit Breaker**: Disables trading for the day if a hard drawdown limit from the start-of-day balance is reached. Isolated securely per MT5 account login.
- **Failed Close Retries**: If a close attempt fails, the ticket is retained and retried on the next loop iteration.
- **Hot-Reloading Configuration**: Tweak risk settings, loop speeds, and caps in `config.yaml` on the fly without restarting the bot.
- **Multi-Account Support**: Switch accounts seamlessly via config. The bot safely isolates memory and tracks daily drawdowns individually per account.
- **Crash-Resilient Loop**: Per-iteration error handling, heartbeat logging, and async Telegram alerts.
- **Optional Filters**: Restrict guardian actions to specific symbols or EA magic numbers.

## Rule Precedence

1. **Daily Circuit Breaker** — if tripped, close all and halt for the day.
2. **Same-Symbol Admission** — decide on the prior same-symbol ticket (close if losing, keep if winning).
3. **Global Two-Position Cap** — evict the oldest remaining ticket until ≤ 2.
4. **SL Enforcement** — set/tighten per-ticket 5% SLs; close unprotectable tickets.
5. **Trailing Equity Stop** — close all if equity drops below the scaled threshold.

## Setup

1. Ensure you have MetaTrader 5 installed and logged into a **hedging-mode** account. **AutoTrading must be enabled** (Green icon) in the terminal options.
2. Install Python 3.11+.
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Copy `config.example.yaml` to `config.yaml` and configure your parameters. The default mode is `observe` for safe rollout.

### Secrets via environment variables

Instead of storing credentials in `config.yaml`, you can set:

| Variable | Config field |
|----------|--------------|
| `MT5_LOGIN` | `account.login` |
| `MT5_PASSWORD` | `account.password` |
| `MT5_SERVER` | `account.server` |
| `TELEGRAM_TOKEN` | `telegram.token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |

`config.yaml` is gitignored. Never commit live passwords.

## Rollout Guide

1. **Observe Mode**: Run the bot on a demo account with `mode: observe` in your config. Open a few trades. Watch the console logs to see what actions the bot *would* have taken.
2. **Live Mode (Demo)**: Switch to `mode: live`. Deliberately test:
   - Same-symbol admission: open two trades on the same symbol — the guardian closes the older loser.
   - Pyramiding a winner: open a new trade on a symbol where the old one is in profit — both kept.
   - Global cap: open a third position — the oldest ticket is closed.
   - 5% per-ticket SL: each ticket gets its own SL at `balance × 5%`.
   - 2→1 peak reset: close one of two positions and verify peak equity resets.
   - Daily circuit breaker.
3. **Live Deployment (Real Funds)**: Only after thorough Observe Mode review and Demo-Live Mode validation, switch to real funds with conservative risk parameters. You can run locally while trading or deploy to a Docker container (see `deploy/`).

## Docker Deployment (Ubuntu)

The `deploy/` directory contains scripts adapted for running the guardian inside a Docker container under Wine on Ubuntu:

```bash
# Inside the container (on drive_c):
cd /config/.wine/drive_c
git clone https://github.com/geevarghesepc/mt5-equity-guardian.git guardian
cd guardian
bash deploy/install.sh

# Health check (no orders):
su -s /bin/bash abc -c "cd /config/.wine/drive_c/guardian && WINEPREFIX=/config/.wine wine C:/Python314/python.exe health.py"

# Start in tmux:
bash deploy/run.sh

# Attach:
su -s /bin/bash abc -c "tmux attach -t guardian"
```

See `deploy/install.sh --help` for host-side Docker deployment.

## Run

```bash
python src/guardian_bot.py
```

Health check (reads terminal state, sends no orders):

```bash
python health.py
```

Logs are written to `logs/guardian.log` with rotation. Audit events are persisted in `state/guardian.db`.
