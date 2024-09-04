# README.md

## Automated Trading Bot with Webhook Integration, Dead easy Basics For Crypto

This code implements a basic automated trading bot that can execute long and short trades on a specified cryptocurrency exchange based on signals received via a webhook. It features trailing stop-loss and emergency exit orders for risk management, and other `why doesn't everyone do this???` features such as limit bactracing to attempt maker ordering.

**Features:**

- **Webhook Triggered Trading:** Execute trades (long entry, short entry, long exit, short exit) based on JSON payloads sent to the `/hook` endpoint.
- **Trailing Stop-Loss:** Dynamically adjusts the stop-loss order as the price moves in your favor.
- **Emergency Exit:** Triggers a market exit if the price drops below a pre-defined percentage threshold, regardless of the trailing stop-loss.
- **Limit Orders with Timeouts:** Optionally use limit orders with a specified cancellation time to avoid slippage.
- **Persistent Storage:** Stores trade history in a SQLite database.
- **Risk Management:** Built-in leverage control and stop-loss mechanisms.
- **Configurable:** Easily customizable through environment variables (see below).

## Requirements:

- Python 3.7+
- `ccxt` library: `pip install ccxt`
- `Flask` library: `pip install Flask`
- `python-dotenv` library: `pip install python-dotenv`
- SQLite3

## Configuration:

**Environment Variables:**

Variable                 | Description                                                                | Default
------------------------ | -------------------------------------------------------------------------- | -------------
`AUTH_ID`                | Authentication ID for webhook requests (**required**)                      |
`TEST_MODE`              | Set to `true` for testing on exchange's testnet, `false` for live trading. | `true`
`EXCHANGE`               | Exchange ID (e.g., `phemex`, `binance`, `kraken`).                         | `phemex`
`TICKER_BASE`            | Base currency of the trading pair (e.g., `BTC`).                           |
`TICKER_QUOTE`           | Quote currency of the trading pair (e.g., `USDT`).                         |
`LEVERAGE`               | Leverage to use for trading.                                               | `1`
`TRAILING_STOP_PERCENT`  | Trailing stop-loss percentage (e.g., `0.02` for 2%).                       | `0.02`
`EMERGENCY_EXIT_PERCENT` | Emergency exit percentage (e.g., `0.05` for 5%).                           | `0.05`
`TRAILING_STOP_TYPE`     | Trailing stop type, either `ByMarkPrice` or `ByLastPrice`.                 | `ByMarkPrice`
`PORT`                   | Port for the Flask webserver.                                              | `8080`

**Exchange API Keys:**

- Set your exchange's API keys as environment variables according to the `EXCHANGE` variable. 

  - For testnet trading, use `[EXCHANGE_ID]_TESTNET_API_KEY` and `[EXCHANGE_ID]_TESTNET_API_SECRET`.
  - For live trading, use `[EXCHANGE_ID]_API_KEY` and `[EXCHANGE_ID]_API_SECRET`.

## Database:

The bot uses a SQLite database (`trading_history.db`) to store trade logs. The schema is automatically created if the database file doesn't exist.

## Webhook Usage:

**Endpoint:** `/hook` **Method:** `POST` **Headers:** `Content-Type: application/json`

**JSON Payload:**

```json
{
  "auth_id": "YOUR_AUTH_ID", // Replace with your configured AUTH_ID
  "action": "long_entry" | "short_entry" | "long_exit" | "short_exit" | "reverse_long_to_short" | "reverse_short_to_long",
  "order_type": "market" | "limit", // Optional, defaults to "market"
  "limit_backtrace_percent": 0.1, // Optional, percentage to backtrace limit orders (e.g., 0.1 for 0.1%)
  "limit_cancel_time_seconds": 60 // Optional, time in seconds to cancel a limit order if not filled
}
```

**Examples:**

**Long Entry (Market Order):**

```json
{
  "auth_id": "YOUR_AUTH_ID",
  "action": "long_entry"
}
```

**Short Exit (Limit Order with 0.2% backtrace and 30-second cancellation):**

```json
{
  "auth_id": "YOUR_AUTH_ID",
  "action": "short_exit",
  "order_type": "limit",
  "limit_backtrace_percent": 0.2,
  "limit_cancel_time_seconds": 30
}
```

## Running the Bot:

1. Configure environment variables (`.env` file or directly in your environment).
2. Run the script: `python trading_bot.py`

**Disclaimer:**

This is a basic implementation of a trading bot and is intended for testing, boilerplate, and educational purposes only. Use at your own risk. Cryptocurrency trading involves significant financial risks. Always do your own research and consider consulting with a financial advisor before making any trading decisions.
