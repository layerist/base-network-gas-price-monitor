import os
import time
import json
import random
import signal
import atexit
import logging
from typing import Optional, Dict, Any

from logging.handlers import RotatingFileHandler
from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException


# ============================================================
#  CONFIGURATION
# ============================================================

class Config:
    PROVIDER_URL: str = os.getenv("PROVIDER_URL", "").strip() or \
                        "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

    FALLBACK_PROVIDERS = [
        "https://base.llamarpc.com",
        "https://base-mainnet.public.blastapi.io",
    ]

    LOG_FILE: str = os.getenv("LOG_FILE", "gas_price_monitor.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", 1))
    MAX_RETRY_DELAY: float = float(os.getenv("MAX_RETRY_DELAY", 30))
    MAX_TOTAL_BACKOFF: float = float(os.getenv("MAX_TOTAL_BACKOFF", 120))

    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 10))
    OUTPUT_JSON: bool = os.getenv("OUTPUT_JSON", "false").lower() == "true"

    COLOR_LOGS: bool = True  # можно выключить


# ============================================================
#  LOGGING SETUP
# ============================================================

class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[94m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[91m",
    }
    RESET = "\033[0m"

    def format(self, record):
        msg = super().format(record)
        if Config.COLOR_LOGS:
            color = self.COLORS.get(record.levelname, "")
            return f"{color}{msg}{self.RESET}"
        return msg


logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))

formatter = ColorFormatter(
    "%(asctime)s | %(levelname)-8s | %(message)s",
    "%Y-%m-%d %H:%M:%S"
)

file_handler = RotatingFileHandler(
    Config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)


# ============================================================
#  WEB3 CLIENT
# ============================================================

class Web3Client:
    """Web3 client with automatic reconnect and fallback providers."""

    def __init__(self, primary_url: str, fallbacks: list[str]):
        self.urls = [primary_url] + fallbacks
        self.web3 = None
        self._connect()

    def _connect(self):
        for url in self.urls:
            try:
                logger.info(f"Connecting to provider: {url}")
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
                if w3.is_connected():
                    logger.info("Connected successfully.")
                    self.web3 = w3
                    return
            except Exception as e:
                logger.warning(f"Failed to connect to {url}: {e}")

        raise ConnectionError("Unable to connect to ANY Web3 provider.")

    def get_web3(self):
        if self.web3 is None or not self.web3.is_connected():
            logger.warning("Web3 disconnected. Reconnecting...")
            self._connect()
        return self.web3


# ============================================================
#  UTILITIES
# ============================================================

def log_json_or_text(data: Dict[str, Any]):
    """Unified output function."""
    if Config.OUTPUT_JSON:
        print(json.dumps(data, ensure_ascii=False))
    else:
        logger.info(
            "Gas [Gwei] total=%.2f | base=%.2f | priority=%.2f | block=%s",
            data["gas_price_gwei"],
            data["base_fee_gwei"] or 0.0,
            data["priority_fee_gwei"] or 0.0,
            data["block_number"],
        )


def exponential_backoff(attempt: int, total_wait: float) -> float:
    """Exponential backoff with jitter."""
    base = Config.RETRY_BASE_DELAY * (2 ** attempt)
    delay = min(base, Config.MAX_RETRY_DELAY)
    wait = min(delay * random.uniform(0.8, 1.2),
               Config.MAX_TOTAL_BACKOFF - total_wait)

    if wait > 0:
        logger.debug(f"Retry #{attempt + 1} in {wait:.2f}s")
        time.sleep(wait)

    return total_wait + wait


def fetch_gas_prices(client: Web3Client) -> Optional[Dict[str, Any]]:
    """Fetch gas prices with retries."""
    total_wait = 0.0

    for attempt in range(Config.RETRY_LIMIT):
        try:
            w3 = client.get_web3()

            gas_price_wei = w3.eth.gas_price
            block = w3.eth.get_block("pending")

            base_fee = block.get("baseFeePerGas") or 0
            priority_fee = gas_price_wei - base_fee if base_fee else None

            result = {
                "gas_price_gwei": float(w3.from_wei(gas_price_wei, "gwei")),
                "base_fee_gwei": float(w3.from_wei(base_fee, "gwei")),
                "priority_fee_gwei": float(w3.from_wei(priority_fee, "gwei")) if priority_fee else None,
                "block_number": block.get("number"),
                "timestamp": int(time.time()),
            }

            log_json_or_text(result)
            return result

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as net_err:
            logger.warning(f"Network error (attempt {attempt + 1}): {net_err}")
        except Exception as e:
            logger.exception(f"Unexpected error: {e}")

        total_wait = exponential_backoff(attempt, total_wait)
        if total_wait >= Config.MAX_TOTAL_BACKOFF:
            break

    logger.error("Failed to fetch gas price after retries.")
    return None


# ============================================================
#  GRACEFUL SHUTDOWN
# ============================================================

class GracefulKiller:
    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGINT, self._handler)
        signal.signal(signal.SIGTERM, self._handler)

    def _handler(self, *_):
        logger.info("Received termination signal. Stopping...")
        self.kill_now = True


# ============================================================
#  MAIN LOOP
# ============================================================

def monitor():
    killer = GracefulKiller()
    client = Web3Client(Config.PROVIDER_URL, Config.FALLBACK_PROVIDERS)

    logger.info("Gas monitor started.")

    while not killer.kill_now:
        try:
            fetch_gas_prices(client)
        except Exception as e:
            logger.exception(f"Monitoring error: {e}")

        for _ in range(Config.MONITOR_INTERVAL * 10):
            if killer.kill_now:
                break
            time.sleep(0.1)

    logger.info("Gas monitor stopped.")


def main():
    atexit.register(lambda: logger.info("Exit."))

    try:
        monitor()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")


if __name__ == "__main__":
    main()
