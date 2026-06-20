# MT5 Stoploss Guardian

An automated guardian script for MetaTrader 5, designed to enforce dynamic equity stoplosses, prevent averaging down, and implement a daily circuit breaker. Built for discretionary manual scalpers.

## Features
- **Dynamic Equity Trailing Stop**: Calculates a stoploss based on peak equity achieved during open positions (e.g. 10% risk for 1 position, 15% risk for multiple).
- **Tiered Trailing**: Tightens the stoploss automatically once a certain profit threshold is hit to lock in break-even.
- **Anti-Averaging Engine & Pyramiding**: Limits max positions and volume per symbol. Can block loser-adding while allowing pyramiding of winners via the `prevent_loser_add` toggle.
- **Daily Circuit Breaker**: Disables trading for the day if a hard drawdown limit from the start-of-day balance is reached. Isolated securely per MT5 account login.
- **Dynamic Broker-Side Failsafe**: Mathematically calculates and places a hard Stop Loss on the broker side for every ticket as a backup against power/internet disconnects.
- **Hot-Reloading Configuration**: Tweak risk settings, loop speeds, and pyramiding flags in `config.yaml` on the fly without restarting the bot.
- **Multi-Account Support**: Switch accounts seamlessly via config. The bot safely isolates memory and tracks daily drawdowns individually per account.
- **Crash-Resilient Loop**: Per-iteration error handling, heartbeat logging, and async Telegram alerts.
- **Optional Filters**: Restrict guardian actions to specific symbols or EA magic numbers.

## Setup
1. Ensure you have MetaTrader 5 installed and logged into an account. **AutoTrading must be enabled** (Green icon) in the terminal options.
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
2. **Live Mode (Demo)**: Switch to `mode: live`. Deliberately test hitting the 10% base stop, the pyramiding rules, and the daily circuit breaker to ensure the bot closes positions as expected.
3. **Live Deployment**: Once verified, you can run this locally while you trade, or deploy to a Windows EC2 instance (e.g. AWS `t3.medium`) to keep it running 24/5.

*Note: There is no native "headless" mode for MT5. For AWS deployments, Windows Server GUI is required. Close charts to save memory.*

## Run
```bash
python src/guardian_bot.py
```

Logs are written to `logs/guardian.log` with rotation. Audit events are persisted in `state/guardian.db`.
