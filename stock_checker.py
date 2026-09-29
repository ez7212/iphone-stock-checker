#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Apple Thailand pickup alerts. Python 3.10+, standard library only (macOS/Linux).

How to run:
  Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID for alerts.
  python3 stock_checker.py --status # check all four variants without sending
  python3 stock_checker.py --test   # one Telegram test; no Apple/state access
  python3 stock_checker.py          # one poll; scheduler handles repetition

Tracks Black and Burgundy, each in 256GB and 512GB, at Central World and Iconsiam.
Part numbers verified against Apple's Thai product page on 2026-09-29.

First observation of available stock alerts immediately. Missing/malformed
responses never reset availability. Only confirmed unavailable stock rearms it.
State defaults beside this script, independent of the scheduler's working dir.
Use only ONE scheduler: local cron and Actions have independent state.

Cron (create ~/.config/iphone-stock.env with
the two export statements, chmod 600; keep secrets outside the repository):
*/15 * * * * /bin/sh -c '. /Users/eric/.config/iphone-stock.env && /Users/eric/iphone-stock-checker/check-stock' >> /Users/eric/iphone-stock-checker/stock-checker.log 2>&1

Delivery is confirmed before state is saved. A crash between Telegram accepting
a message and durable state storage (or a failed Actions push) can cause a repeat;
Telegram sendMessage has no idempotency key. Ordinary restarts suppress repeats.
"""

from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, TypeAlias
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

JSON: TypeAlias = "dict[str, JSON] | list[JSON] | str | int | float | bool | None"
USER_AGENT: Final = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
APPLE_URL: Final = "https://www.apple.com/th/shop/retail/pickup-message"
BUY_URL: Final = "https://www.apple.com/th/shop/buy-iphone/iphone-18-pro"
PRODUCTS: Final = (
    ("MJXN4ZP/A", "256GB · Black"),
    ("MJXT4ZP/A", "512GB · Black"),
    ("MJXQ4ZP/A", "256GB · Burgundy"),
    ("MJXV4ZP/A", "512GB · Burgundy"),
)


class CheckerError(Exception):
    """An actionable configuration, response, or persistence failure."""


@dataclass(frozen=True, slots=True)
class Stock:
    key: str
    store: str
    part: str
    capacity: str
    available: bool
    quote: str


def mapping(value: JSON) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise CheckerError("Expected a JSON object")
    return value


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise CheckerError(f"Set {name} before running")
    return value


def request_json(request: Request) -> JSON:
    """Do not include request URLs in errors: Telegram URLs contain the token."""
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        raise CheckerError(f"HTTP request failed (status {exc.code})") from None
    except (URLError, TimeoutError, OSError):
        raise CheckerError("HTTP request failed (network/TLS/timeout)") from None
    except (ValueError, UnicodeError):
        raise CheckerError("HTTP response was not valid JSON") from None


def fetch_stock(part: str, capacity: str) -> list[Stock]:
    query = urlencode({"pl": "true", "mts.0": "regular", "parts.0": part,
                       "location": "10330"})
    payload = mapping(request_json(Request(
        f"{APPLE_URL}?{query}", headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )))
    body = mapping(payload.get("body"))
    stores = body.get("stores")
    if not isinstance(stores, list):
        raise CheckerError("Apple response is missing the stores list")
    result: list[Stock] = []
    matched: set[str] = set()
    for raw in stores:
        store = mapping(raw)
        name = store.get("storeName")
        if not isinstance(name, str):
            raise CheckerError("Apple response contains an invalid store name")
        target = next((s for s in ("central", "iconsiam") if s in name.casefold()), None)
        if target is None:
            continue
        if target in matched:
            raise CheckerError("Apple response contains duplicate Bangkok stores")
        matched.add(target)
        availability = mapping(mapping(store.get("partsAvailability")).get(part))
        display = availability.get("pickupDisplay")
        if display not in ("available", "unavailable"):
            raise CheckerError("Apple returned an unknown pickup status; state was preserved")
        quote = ""
        if display == "available":
            regular = mapping(mapping(availability.get("messageTypes")).get("regular"))
            raw_quote = (regular.get("storePickupQuote") or regular.get("pickupSearchQuote")
                         or availability.get("pickupSearchQuote"))
            if not isinstance(raw_quote, str) or not raw_quote.strip():
                raise CheckerError("Available stock is missing its pickup quote")
            quote = html.unescape(re.sub(r"<[^>]+>", "", raw_quote)).strip()
        result.append(Stock(f"{target}|{part}", name, part, capacity,
                            display == "available", quote))
    if matched != {"central", "iconsiam"}:
        raise CheckerError("Apple response did not include both Bangkok stores")
    return result


def read_state(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    payload = mapping(json.loads(path.read_text(encoding="utf-8")))
    if payload.get("version") != 1:
        raise CheckerError("Unsupported state format; restore a valid state file")
    pairs = mapping(payload.get("pairs"))
    result: dict[str, bool] = {}
    for key, value in pairs.items():
        if not isinstance(value, bool):
            raise CheckerError("Invalid state value; restore a valid state file")
        result[key] = value
    return result


def save_state(path: Path, pairs: dict[str, bool]) -> None:
    """Replace atomically so interrupted writes cannot truncate the last state."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({"version": 1, "pairs": pairs}, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = urlencode({"chat_id": chat_id, "text": text}).encode()
    response = mapping(request_json(Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
        headers={"User-Agent": USER_AGENT},
    )))
    if response.get("ok") is not True:
        raise CheckerError("Telegram rejected the message; check bot token/chat ID")


def run(path: Path) -> None:
    token, chat_id = required_env("TELEGRAM_BOT_TOKEN"), required_env("TELEGRAM_CHAT_ID")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        previous = read_state(path)
        # Parse every response before any notification or state change.
        stocks = [stock for part, label in PRODUCTS for stock in fetch_stock(part, label)]
        print_stock(stocks)
        state = previous.copy()
        for stock in stocks:
            if not stock.available:
                state[stock.key] = False
        save_state(path, state)  # Check persistence before sending anything.
        for stock in stocks:
            if not stock.available or previous.get(stock.key, False):
                continue
            send_telegram(token, chat_id, (
                f"IN STOCK\n{stock.store}\niPhone 18 Pro Max · {stock.capacity}"
                f"\n{stock.part}\n{stock.quote}\n{BUY_URL}"
            ))
            state[stock.key] = True
            save_state(path, state)  # Preserve successful alerts if a later send fails.


def print_stock(stocks: list[Stock]) -> None:
    print(f"Checked {datetime.now().astimezone().isoformat(timespec='seconds')}", flush=True)
    for stock in stocks:
        status = "IN STOCK" if stock.available else "Unavailable"
        print(f"{stock.store:16} {stock.capacity:18} {status} {stock.quote}".rstrip(), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="Send one Telegram test")
    mode.add_argument("--status", action="store_true", help="Show stock without alerts or state changes")
    parser.add_argument("--watch", type=int, metavar="SECONDS",
                        help="Repeat checks, at least 300 seconds apart; Ctrl-C stops")
    parser.add_argument("--state-file", type=Path,
                        default=Path(__file__).resolve().with_name("stock-state.json"))
    args = parser.parse_args()
    if args.watch is not None and (args.watch < 300 or args.test):
        parser.error("--watch requires at least 300 seconds and cannot be used with --test")
    try:
        if args.test:
            send_telegram(required_env("TELEGRAM_BOT_TOKEN"), required_env("TELEGRAM_CHAT_ID"),
                          "Test: Apple Thailand pickup stock checker Telegram connection works.")
        else:
            while True:
                try:
                    if args.status:
                        print_stock([s for part, label in PRODUCTS for s in fetch_stock(part, label)])
                    else:
                        run(args.state_file.resolve())
                except (CheckerError, OSError, ValueError) as exc:
                    if args.watch is None:
                        raise
                    print(f"stock checker: check failed, retrying next interval: {exc}",
                          file=sys.stderr, flush=True)
                if args.watch is None:
                    break
                time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0
    except (CheckerError, OSError, ValueError) as exc:
        print(f"stock checker: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
