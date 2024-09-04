import asyncio
import csv
import json
import os
import sqlite3
import sys
import threading
import time
from typing import Dict, List, NoReturn, Union
import pandas as pd
import ccxt
from flask import Flask, jsonify, request
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- Settings ---
app = Flask(__name__)
DATABASE = "trading_history.db"
AUTH_ID = os.getenv("AUTH_ID")
TEST_MODE = os.getenv("TEST_MODE", "true").lower() == "true"
EXCHANGE_ID = os.getenv("EXCHANGE", "phemex").lower()
TICKER_BASE = os.getenv("TICKER_BASE")
TICKER_QUOTE = os.getenv("TICKER_QUOTE")
TICKER = f"{TICKER_BASE}/{TICKER_QUOTE}"
LEVERAGE = float(os.getenv("LEVERAGE", 1))

TRAILING_STOP_PERCENT = float(os.getenv("TRAILING_STOP_PERCENT", 0.02))  # 2%
EMERGENCY_EXIT_PERCENT = float(os.getenv("EMERGENCY_EXIT_PERCENT", 0.05))  # 5%
TRAILING_STOP_TYPE = os.getenv(
    "TRAILING_STOP_TYPE", "ByMarkPrice"
)  # "ByMarkPrice" or "ByLastPrice"
MAX_RETRIES = 3

# --- Database Functions ---


def get_db():
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with app.app_context():
        db = get_db()
        try:
            with app.open_resource("schema.sql", mode="r") as f:
                db.cursor().executescript(f.read())
        except:
            pass
        db.commit()
        db.close()


# Initialize the database
init_db()

# --- Exchange Setup ---
exchange_class = getattr(ccxt, EXCHANGE_ID)
exchange = exchange_class(
    {
        "apiKey": os.getenv(
            f"{EXCHANGE_ID.upper()}_TESTNET_API_KEY"
            if TEST_MODE
            else f"{EXCHANGE_ID.upper()}_API_KEY"
        ),
        "secret": os.getenv(
            f"{EXCHANGE_ID.upper()}_TESTNET_API_SECRET"
            if TEST_MODE
            else f"{EXCHANGE_ID.upper()}_API_SECRET"
        ),
        "enableRateLimit": True,
        "timeout": 30000,
    }
)

if TEST_MODE:
    if hasattr(exchange, "urls") and "test" in exchange.urls:
        exchange.urls["api"] = exchange.urls["test"]
    print(f"Currently TESTING on {EXCHANGE_ID}")
else:
    print(f"Currently LIVE on {EXCHANGE_ID}")

exchange.load_markets()

# --- Global State ---
current_positions: Dict = {}
last_prices: Dict = {}


# --- Helper Functions ---
def log_trade(data: Dict):
    db = get_db()
    db.execute(
        "INSERT INTO trades (timestamp, action, order_type, symbol, price, amount, fees, status)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(time.time()),
            data.get("action"),
            data.get("order_type"),
            data.get("symbol"),
            data.get("price"),
            data.get("amount"),
            data.get("fees"),
            data.get("status"),
        ),
    )
    db.commit()
    db.close()


def calculate_order_price(
    action: str, quote_price: float, limit_backtrace_percent: float = None
) -> float:
    if limit_backtrace_percent is not None:
        limit_backtrace_percent = float(limit_backtrace_percent) * 0.01
        if action in ("short_entry", "short_exit", "reverse_long_to_short"):
            return quote_price * (1 - limit_backtrace_percent)
        else:
            return quote_price * (1 + limit_backtrace_percent)
    return quote_price


async def handle_limit_order_fill(
    exchange: ccxt.Exchange,
    order_id: str,
    amount: float,
    limit_cancel_time_seconds: int,
):
    await asyncio.sleep(limit_cancel_time_seconds)
    try:
        order : NoReturn = await await exchange.fetch_order(order_id, TICKER)
        if order["status"] != "closed":
            exchange.cancel_order(order_id, TICKER)
            return False, amount - order["filled"]
        return True, 0
    except ccxt.NetworkError as e:
        print(f"Network error checking order: {e}")
        return False, amount


async def place_order_with_retries(
    exchange: ccxt.Exchange,
    order_type: str,
    side: str,
    amount: float,
    price: float,
    params: dict = {},
    retries: int = 3,
    delay: int = 2,
):
    for i in range(retries):
        try:
            if order_type == "limit":
                order_book: NoReturn = await exchange.fetch_order_book(TICKER)
                if side == "buy":
                    best_bid = order_book["bids"][0][0] if order_book["bids"] else None
                    if best_bid and price <= best_bid:
                        price = best_bid
                elif side == "sell":
                    best_ask = order_book["asks"][0][0] if order_book["asks"] else None
                    if best_ask and price >= best_ask:
                        price = best_ask

            order = await exchange.create_order(
                TICKER, order_type, side, amount, price, params
            )
            return order
        except ccxt.NetworkError as e:
            print(f"Network error placing order: {e}. Retry {i+1}/{retries}")
            if i < retries - 1:
                await asyncio.sleep(delay * 2**i)
    raise Exception("Failed to place order after retries")


# --- Trading Logic ---
async def execute_trade(json_data: Dict):
    action = json_data.get("action")
    symbol = TICKER
    order_type = json_data.get("order_type", "market")
    limit_backtrace_percent = json_data.get("limit_backtrace_percent")
    limit_cancel_time_seconds = int(json_data.get("limit_cancel_time_seconds", 0))

    try:
        ticker = await exchange.fetch_ticker(symbol)
        last_price = ticker["last"]
        last_prices[TICKER] = last_price
    except Exception as e:
        print(f"Error fetching ticker data: {e}")
        return

    order_price = calculate_order_price(action, last_price, limit_backtrace_percent)

    try:
        balance = await exchange.fetch_balance()
        free_balance = (
            balance[TICKER_QUOTE]["free"]
            if "USDT" in TICKER
            else balance[TICKER_BASE]["free"] * last_price
        )
        amount = (free_balance * LEVERAGE * 0.95) / last_price

        if order_type == "limit":
            order = await place_order_with_retries(
                exchange,
                "limit",
                "buy" if action == "long_entry" else "sell",
                amount,
                order_price,
            )
            if limit_cancel_time_seconds > 0:
                filled, remaining_amount = await handle_limit_order_fill(
                    exchange, order["id"], amount, limit_cancel_time_seconds
                )
                if not filled:
                    print("Limit order did not fill in time, canceling order.")
                    amount = remaining_amount
        else:
            order = await place_order_with_retries(
                exchange,
                "market",
                "buy" if action == "long_entry" else "sell",
                amount,
                order_price,
            )

        if order:
            current_positions[symbol] = {
                "side": "long" if action == "long_entry" else "short",
                "entry_price": order_price,
                "amount": amount,
                "trailing_stop": None,  # We'll set this dynamically later
                "emergency_exit": order_price
                * (
                    1 - EMERGENCY_EXIT_PERCENT
                    if action == "long_entry"
                    else 1 + EMERGENCY_EXIT_PERCENT
                ),
            }
            print(
                f"{action.upper()} order placed for {symbol} at {order_price}. Amount: {amount}"
            )

        log_trade(
            {
                "action": action,
                "order_type": order_type,
                "symbol": symbol,
                "price": order_price,
                "amount": amount,
                "fees": order["fees"] if order else "N/A",
                "status": "placed"
                if order
                else f"error: {order.get('error', 'Order Failed')}",
            }
        )
        return "Order placed", 200

    except Exception as e:
        log_trade(
            {
                "action": action,
                "order_type": order_type,
                "symbol": symbol,
                "price": order_price,
                "amount": 0,
                "fees": "N/A",
                "status": f"error: {e}",
            }
        )
        return f"Error placing order: {e}", 500


async def manage_position(symbol: str, last_price: float):
    if symbol in current_positions:
        position = current_positions[symbol]
        if position["side"] == "long":
            await manage_long_position(symbol, last_price)
        elif position["side"] == "short":
            await manage_short_position(symbol, last_price)


async def manage_long_position(symbol: str, last_price: float):
    position = current_positions[symbol]
    # Dynamically adjust trailing stop
    if position["trailing_stop"] is None or last_price >= position["trailing_stop"]:
        new_trailing_stop = last_price * (1 - TRAILING_STOP_PERCENT)
        if (
            position["trailing_stop"] is None
            or new_trailing_stop > position["trailing_stop"]
        ):  # Only update if the new stop is higher
            position["trailing_stop"] = new_trailing_stop
            print(f"Trailing stop for {symbol} updated to: {position['trailing_stop']}")

            # Cancel existing trailing stop order (if any)
            if "trailing_stop_order_id" in position:
                try:
                    await exchange.cancel_order(
                        position["trailing_stop_order_id"], symbol
                    )
                except Exception as e:
                    print(f"Error canceling trailing stop order: {e}")

            # Place new trailing stop order
            try:
                trailing_stop_order_params = {
                    "stopLoss": {
                        "triggerPriceType": TRAILING_STOP_TYPE,
                        "triggerPrice": position["trailing_stop"],
                        "pegOffsetValueRp": int(
                            TRAILING_STOP_PERCENT
                            * position["entry_price"]
                            * exchange.markets[TICKER]["precision"]["price"]
                        ),  # Specify the trailing offset in raw price units
                    }
                }
                order = await exchange.create_order(
                    symbol,
                    "stop",
                    "sell",
                    position["amount"],
                    params=trailing_stop_order_params,
                )
                position["trailing_stop_order_id"] = order[
                    "id"
                ]  # Store the order ID for cancellation
                print(
                    f"Trailing stop order placed for {symbol} at {position['trailing_stop']}"
                )
            except Exception as e:
                print(f"Error placing trailing stop order: {e}")
    if (
        last_price <= position["trailing_stop"]
        or last_price <= position["emergency_exit"]
    ):
        print(f"Exiting long position for {symbol} at market price.")
        try:
            order = await exchange.create_order(
                symbol, "market", "sell", position["amount"], last_price
            )
            log_trade(
                {
                    "action": "long_exit",
                    "order_type": "market",
                    "symbol": symbol,
                    "price": last_price,
                    "amount": position["amount"],
                    "fees": order["fees"] if order else "N/A",
                    "status": "placed" if order else "error: Order Failed",
                }
            )
            del current_positions[symbol]  # Remove position from tracking
        except Exception as e:
            print(f"Error exiting long position: {e}")


async def manage_short_position(symbol: str, last_price: float):
    position = current_positions[symbol]

    # Dynamically adjust trailing stop
    if position["trailing_stop"] is None or last_price <= position["trailing_stop"]:
        new_trailing_stop = last_price * (1 + TRAILING_STOP_PERCENT)
        if (
            position["trailing_stop"] is None
            or new_trailing_stop < position["trailing_stop"]
        ):  # Only update if the new stop is lower
            position["trailing_stop"] = new_trailing_stop
            print(f"Trailing stop for {symbol} updated to: {position['trailing_stop']}")

            # Cancel existing trailing stop order (if any)
            if "trailing_stop_order_id" in position:
                try:
                    await exchange.cancel_order(
                        position["trailing_stop_order_id"], symbol
                    )
                except Exception as e:
                    print(f"Error canceling trailing stop order: {e}")

            # Place new trailing stop order
            try:
                trailing_stop_order_params = {
                    "stopLoss": {
                        "triggerPriceType": TRAILING_STOP_TYPE,
                        "triggerPrice": position["trailing_stop"],
                        "pegOffsetValueRp": int(
                            TRAILING_STOP_PERCENT
                            * position["entry_price"]
                            * exchange.markets[TICKER]["precision"]["price"]
                        ),  # Specify the trailing offset in raw price units
                    }
                }
                order = await exchange.create_order(
                    symbol,
                    "stop",
                    "buy",  # Buy to cover the short position
                    position["amount"],
                    params=trailing_stop_order_params,
                )
                position["trailing_stop_order_id"] = order[
                    "id"
                ]  # Store the order ID for cancellation
                print(
                    f"Trailing stop order placed for {symbol} at {position['trailing_stop']}"
                )
            except Exception as e:
                print(f"Error placing trailing stop order: {e}")
    if (
        last_price >= position["trailing_stop"]
        or last_price >= position["emergency_exit"]
    ):
        print(f"Exiting short position for {symbol} at market price.")
        try:
            order = await exchange.create_order(
                symbol, "market", "buy", position["amount"], last_price
            )
            log_trade(
                {
                    "action": "short_exit",
                    "order_type": "market",
                    "symbol": symbol,
                    "price": last_price,
                    "amount": position["amount"],
                    "fees": order["fees"] if order else "N/A",
                    "status": "placed" if order else "error: Order Failed",
                }
            )
            del current_positions[symbol]  # Remove position from tracking
        except Exception as e:
            print(f"Error exiting short position: {e}")


# --- Data Handling ---
async def fetch_ohlcv_data(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    since: Union[int, str],
    limit: int,
) -> pd.DataFrame:
    """Fetches OHLCV data and returns it as a pandas DataFrame."""
    if isinstance(since, str):
        since = exchange.parse8601(since) or
    ohlcv = scrape_ohlcv(exchange, symbol, timeframe, since, limit)
    df = pd.DataFrame(
        ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)
    return df


def save_to_csv(filename: str, df: pd.DataFrame):
    """Saves a pandas DataFrame to a CSV file."""
    df.to_csv(filename)
    print(f"Saved {len(df)} candles to {filename}")


# --- Exchange Interactions ---


async def cancel_order(exchange: ccxt.Exchange, order_id: str, symbol: str) -> dict:
    """Cancels an order."""
    try:
        canceled_order = await exchange.cancel_order(order_id, symbol)
        print(f"Order canceled: {canceled_order}")
        return canceled_order
    except Exception as e:
        handle_exception(e)


async def fetch_account_balance(exchange: ccxt.Exchange) -> dict:
    """Fetches and prints the account balance."""
    try:
        balance = await exchange.fetch_balance()
        print(f"{exchange.id} balance: {balance}")
        return balance
    except Exception as e:
        handle_exception(e)


async def fetch_orderbook(exchange: ccxt.Exchange, symbol: str) -> dict:
    """Fetches and prints the order book."""
    try:
        orderbook = await exchange.fetch_order_book(symbol)
        print(f"{exchange.id} order book: {orderbook}")
        return orderbook
    except Exception as e:
        handle_exception(e)


async def fetch_ticker(exchange: ccxt.Exchange, symbol: str) -> dict:
    """Fetches and prints the ticker."""
    try:
        ticker = await exchange.fetch_ticker(symbol)
        print(f"{exchange.id} ticker: {ticker}")
        return ticker
    except Exception as e:
        handle_exception(e)


async def fetch_open_orders(exchange: ccxt.Exchange, symbol: str = None) -> List[dict]:
    """Fetches and prints all open orders."""
    try:
        orders = await exchange.fetch_open_orders(symbol)
        print(f"{exchange.id} open orders: {orders}")
        return orders
    except Exception as e:
        handle_exception(e)


async def fetch_trades(exchange: ccxt.Exchange, symbol: str) -> List[dict]:
    """Fetches and prints all trades."""
    try:
        trades = await exchange.fetch_trades(symbol)
        print(f"{exchange.id} trades: {trades}")
        return trades
    except Exception as e:
        handle_exception(e)


async def fetch_positions(exchange: ccxt.Exchange) -> List[dict]:
    """Fetches and prints account positions."""
    try:
        positions = await exchange.fetch_positions()
        print(f"Positions: {positions}")
        return positions
    except Exception as e:
        handle_exception(e)


async def create_trailing_amount_order(
    exchange: ccxt.Exchange,
    symbol: str,
    order_type: str,
    side: str,
    amount: float,
    price: float,
    trailing_amount: float,
) -> dict:
    """Creates a trailing amount order."""
    try:
        params = {"trailingAmount": trailing_amount}
        order = await exchange.create_order(
            symbol, order_type, side, amount, price, params
        )
        print("Trailing amount order created:", order)
        return order
    except Exception as e:
        handle_exception(e)


async def create_trailing_percent_order(
    exchange: ccxt.Exchange,
    symbol: str,
    order_type: str,
    side: str,
    amount: float,
    price: float,
    trailing_percent: float,
) -> dict:
    """Creates a trailing percent order."""
    try:
        params = {"trailingPercent": trailing_percent}
        order = await exchange.create_order(
            symbol, order_type, side, amount, price, params
        )
        print("Trailing percent order created:", order)
        return order
    except Exception as e:
        handle_exception(e)


async def borrow_margin(
    exchange: ccxt.Exchange,
    borrow_coin: str,
    amount_to_borrow: float,
    symbol: str,
    margin_mode: str,
) -> dict:
    """Borrows margin."""
    try:
        params = {"marginMode": margin_mode}
        borrow_result = await exchange.borrow_margin(
            borrow_coin, amount_to_borrow, symbol, params
        )
        print(f"Margin borrowed: {borrow_result}")
        return borrow_result
    except Exception as e:
        handle_exception(e)


async def repay_margin(
    exchange: ccxt.Exchange,
    repay_coin: str,
    amount_to_repay_back: float,
    symbol: str,
    margin_mode: str,
) -> dict:
    """Repays margin."""
    try:
        params = {"marginMode": margin_mode}
        repay_result = await exchange.repay_margin(
            repay_coin, amount_to_repay_back, symbol, params
        )
        print(f"Margin repaid: {repay_result}")
        return repay_result
    except Exception as e:
        handle_exception(e)


async def fetch_deposit_address(
    exchange: ccxt.Exchange, code: str, params: Dict = {}
) -> dict:
    """Fetches the deposit address."""
    try:
        deposit = await exchange.fetch_deposit_address(code, params)
        print(f"Deposit address: {deposit}")
        return deposit
    except Exception as e:
        handle_exception(e)


async def withdraw(
    exchange: ccxt.Exchange,
    code: str,
    amount: float,
    address: str,
    tag: str = None,
    params: Dict = {},
) -> dict:
    """Withdraws funds."""
    try:
        withdrawal = await exchange.withdraw(code, amount, address, tag, params)
        print(f"Withdrawal: {withdrawal}")
        return withdrawal
    except Exception as e:
        handle_exception(e)


async def create_oco_order(
    exchange: ccxt.Exchange,
    symbol: str,
    side: str,
    amount: float,
    price: float,
    stop_price: float,
    stop_limit_price: float,
) -> dict:
    """Creates an OCO order (One-Cancels-The-Other)."""
    try:
        market = exchange.market(symbol)
        params = {
            "symbol": market["id"],
            "side": side,  # SELL, BUY
            "quantity": exchange.amount_to_precision(symbol, amount),
            "price": exchange.price_to_precision(symbol, price),
            "stopPrice": exchange.price_to_precision(symbol, stop_price),
            "stopLimitPrice": exchange.price_to_precision(symbol, stop_limit_price),
            "stopLimitTimeInForce": "GTC",  # GTC, FOK, IOC
            # ... (Add other OCO params as needed) ...
        }
        response = await exchange.private_post_order_oco(params)
        print(f"OCO order created: {response}")
        return response
    except Exception as e:
        handle_exception(e)


async def fetch_tickers_concurrent(
    exchange: ccxt.Exchange, symbols: List[str]
) -> List[dict]:
    """Fetches tickers for multiple symbols concurrently."""
    try:
        await exchange.load_markets()
        print(f"{exchange.id} fetching all tickers concurrently")
        tasks = [fetch_ticker(exchange, symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for ticker, symbol in zip(results, symbols):
            if isinstance(ticker, Exception):
                print(f"{exchange.id} {symbol} error: {ticker}")
            else:
                print(f"{exchange.id} {symbol} ok")
        return results
    except Exception as e:
        handle_exception(e)


# --- Utility Functions ---
def retry_fetch_ohlcv(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, since: int, limit: int
) -> List:
    """Retries fetching OHLCV data with a given number of attempts."""
    for _ in range(MAX_RETRIES):
        try:
            return exchange.fetch_ohlcv(symbol, timeframe, since, limit)
        except Exception as e:
            print(f"Error fetching OHLCV data: {e}. Retrying...")
            time.sleep(1)  # Wait before retrying
    print(f"Failed to fetch OHLCV data after {MAX_RETRIES} attempts.")
    return None  # Or raise an exception if you prefer


def scrape_ohlcv(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, since: int, limit: int
) -> List:
    """Scrapes OHLCV data from the exchange."""
    timeframe_duration_in_ms = exchange.parse_timeframe(timeframe) * 1000
    timedelta = limit * timeframe_duration_in_ms
    now = exchange.milliseconds()
    all_ohlcv = []
    fetch_since = since
    while fetch_since < now:
        ohlcv = retry_fetch_ohlcv(exchange, symbol, timeframe, fetch_since, limit)
        if ohlcv:
            fetch_since = (
                (ohlcv[-1][0] + 1) if len(ohlcv) else (fetch_since + timedelta)
            )
            all_ohlcv.extend(ohlcv)
            print(
                f"{len(all_ohlcv)} candles in total from {exchange.iso8601(all_ohlcv[0][0])} to {exchange.iso8601(all_ohlcv[-1][0])}"
            )
    return exchange.filter_by_since_limit(all_ohlcv, since, None, key=0)


def handle_exception(e: Exception):
    """Handles exceptions with more context."""
    print(f"An error occurred: {type(e).__name__} - {e}")
    # Add logging or more specific error handling here


# --- Text Styling ---
def style_text(text: str, code: str) -> str:
    """Applies ANSI escape codes for text styling."""
    return f"\033[{code}m{text}\033[0m"


def green(text: str) -> str:
    return style_text(text, "92")


def blue(text: str) -> str:
    return style_text(text, "94")


def yellow(text: str) -> str:
    return style_text(text, "93")


def red(text: str) -> str:
    return style_text(text, "91")


def pink(text: str) -> str:
    return style_text(text, "95")


def bold(text: str) -> str:
    return style_text(text, "1")


def underline(text: str) -> str:
    return style_text(text, "4")


def print_supported_exchanges():
    """Prints a list of supported exchanges."""
    print(f"Supported exchanges: {green(', '.join(ccxt.exchanges))}")


def print_usage():
    """Prints usage instructions for the script."""
    print(f"Usage: python {sys.argv[0]} {green('exchange_id')} {yellow('[symbol]')}")
    print("Symbol is optional, for example:")
    print(f"python {sys.argv[0]} {green('kraken')}")
    print(f"python {sys.argv[0]} {green('coinbasepro')} {yellow('BTC/USD')}")
    print_supported_exchanges()


# --- Webhook Route ---
@app.route("/hook", methods=["POST"])
async def webhook_handler():
    json_data = request.get_json()
    await handle_trade_signal(json_data)
    return jsonify({"status": "ok"}), 200


async def handle_trade_signal(json_data: Dict):
    if json_data.get("auth_id") == AUTH_ID:
        await execute_trade(json_data)


# --- Main Loop ---
async def main_loop():
    while True:
        try:
            ticker = await exchange.fetch_ticker(TICKER)
            last_price = ticker["last"]
            last_prices[TICKER] = last_price

            await manage_position(TICKER, last_price)

            await asyncio.sleep(1)  # Adjust as needed
        except Exception as e:
            print(f"Error fetching ticker data: {e}")


# --- Main ---
if __name__ == "__main__":
    # Start the Flask app in a separate thread
    flask_thread = threading.Thread(
        target=app.run, kwargs={"debug": True, "port": int(os.getenv("PORT", 8080))}
    )
    flask_thread.daemon = True
    flask_thread.start()

    # Run the main trading loop using asyncio
    loop = asyncio.get_event_loop()
    loop.create_task(main_loop())
    loop.run_forever()
