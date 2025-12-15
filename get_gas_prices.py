#!/usr/bin/env python3
"""
Robust gas price monitor for EVM networks (Base by default).

Improvements:
- Stronger typing and validation
- Cleaner logging initialization (no duplicate handlers)
- Better provider rotation and health checks
- Safer fee calculation for EIP-1559 and legacy blocks
- Centralized retry / backoff logic
- Minor performance and readability improvements
"""

from __future__ import annotations

import os
import time
import json
import random
import signal
import atexit
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from logging.handlers import RotatingFileHandler
from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException


# ============================================================
#  CONFIGURATION
# ============================================================

@dataclass(frozen=True)
class Config:
    PROVIDER_URL: str = os.getenv(
        "PROVIDER_URL",
        "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID",
    ).strip()

    FALLBACK_PROVIDERS: List[str] = (
        "https://base.llamarpc.com",
        "https://base-mainnet.public.blastapi.io",
    )

    LOG_FILE: str = os.getenv("LOG_FILE", "gas_price_monitor.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", 1.0))
    MAX_RETRY_DELAY: float = float(os.getenv("MAX_RETRY_DELAY", 30.0))
    MAX_TOTAL_BACKOFF: float = float(os.getenv("MAX_TOTAL_BACKOFF", 120.0))

    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 10))
    OUTPUT_JSON: bool = os.getenv("OUTPUT_JSON", "false").lower() == "true"

    COLOR_LOGS: bool = os.getenv("COLOR_LOGS", "true").lower() == "true"
    HTTP_TIMEOUT: int = int(os.getenv("HTTP_TIMEOUT", 10))


CFG = Config()


# ============================================================
#  LOGGING
# ============================================================

class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[94m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if CFG.COLOR_LOGS:
            return f"{self.COLORS.get(record.levelname, '')}{msg}{self.RESET}"
        return msg


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("GasPriceMonitor")
    logger.setLevel(getattr(logging, CFG.LOG_LEVEL, logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = ColorFormatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        CFG.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


logger = setup_logger()


# ============================================================
#  WEB3 CLIENT
# ============================================================

class Web3Client:
    """Web3 client with automatic reconnect and provider rotation."""

    def __init__(self, primary_url: str, fallbacks: List[str]):
        self.urls = [primary_url, *fallbacks]
        self._index = 0
        self.web3: Optional[Web3] = None
        self._connect()

    def _connect(self) -> None:
        for _ in range(len(self.urls)):
            url = self.urls[self._index]
            self._index = (self._index + 1) % len(self.urls)

            try:
                logger.info(f"Connecting to provider: {url}")
                w3 = Web3(
                    Web3.HTTPProvider(
                        url,
                        request_kwargs={"timeout": CFG.HTTP_TIMEOUT},
                    )
                )
                if w3.is_connected():
                    self.web3 = w3
                    logger.info("Provider connected successfully.")
                    return
            except Exception as e:
                logger.warning(f"Connection failed: {e}")

        raise ConnectionError("All Web3 providers are unavailable.")

    def get(self) -> Web3:
        if not self.web3 or not self.web3.is_connected():
            logger.warning("Web3 disconnected, reconnecting...")
            self._connect()
        return self.web3


# ============================================================
#  HELPERS
# ============================================================

def output(data: Dict[str, Any]) -> None:
    if CFG.OUTPUT_JSON:
        print(json.dumps(data, ensure_ascii=False))
    else:
        logger.info(
            "Gas [Gwei] total=%.2f | base=%.2f | priority=%.2f | block=%s",
            data["gas_price_gwei"],
            data["base_fee_gwei"],
            data["priority_fee_gwei"],
            data["block_number"],
        )


def backoff(attempt: int, total_wait: float) -> float:
    delay = min(
        CFG.RETRY_BASE_DELAY * (2 ** attempt),
        CFG.MAX_RETRY_DELAY,
    )
    jitter = random.uniform(0.8, 1.2)
    wait = min(delay * jitter, CFG.MAX_TOTAL_BACKOFF - total_wait)

    if wait > 0:
        logger.debug(f"Retry in {wait:.2f}s")
        time.sleep(wait)

    return total_wait + wait


# ============================================================
#  CORE LOGIC
# ============================================================

def fetch_gas_prices(client: Web3Client) -> Optional[Dict[str, Any]]:
    total_wait = 0.0

    for attempt in range(CFG.RETRY_LIMIT):
        try:
            w3 = client.get()

            gas_price = w3.eth.gas_price
            block = w3.eth.get_block("pending")

            base_fee = block.get("baseFeePerGas") or 0
            priority_fee = max(gas_price - base_fee, 0)

            result = {
                "gas_price_gwei": float(w3.from_wei(gas_price, "gwei")),
                "base_fee_gwei": float(w3.from_wei(base_fee, "gwei")),
                "priority_fee_gwei": float(w3.from_wei(priority_fee, "gwei")),
                "block_number": block.get("number"),
                "timestamp": int(time.time()),
            }

            output(result)
            return result

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logger.warning(f"Network error (attempt {attempt + 1}): {e}")
        except Exception:
            logger.exception("Unexpected error")

        total_wait = backoff(attempt, total_wait)
        if total_wait >= CFG.MAX_TOTAL_BACKOFF:
            break

    logger.error("Gas price fetch failed after retries.")
    return None


# ============================================================
#  GRACEFUL SHUTDOWN
# ============================================================

class GracefulShutdown:
    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_: Any) -> None:
        logger.info("Shutdown signal received.")
        self.stop = True


# ============================================================
#  MAIN LOOP
# ============================================================

def monitor() -> None:
    shutdown = GracefulShutdown()
    client = Web3Client(CFG.PROVIDER_URL, list(CFG.FALLBACK_PROVIDERS))

    logger.info("Gas monitor started.")

    while not shutdown.stop:
        fetch_gas_prices(client)
        for _ in range(CFG.MONITOR_INTERVAL * 10):
            if shutdown.stop:
                break
            time.sleep(0.1)

    logger.info("Gas monitor stopped.")


def main() -> None:
    atexit.register(lambda: logger.info("Process exiting."))
    monitor()


if __name__ == "__main__":
    main()
