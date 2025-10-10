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


# === Configuration ===
class Config:
    """Centralized configuration with environment overrides."""
    PROVIDER_URL: str = os.getenv("PROVIDER_URL", "").strip() or "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"
    LOG_FILE: str = os.getenv("LOG_FILE", "gas_price_monitor.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: int = int(os.getenv("RETRY_BASE_DELAY", 1))
    MAX_RETRY_DELAY: int = int(os.getenv("MAX_RETRY_DELAY", 30))
    MAX_TOTAL_BACKOFF: int = int(os.getenv("MAX_TOTAL_BACKOFF", 120))
    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 10))
    OUTPUT_JSON: bool = os.getenv("OUTPUT_JSON", "false").lower() == "true"


# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s")

file_handler = RotatingFileHandler(Config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)


# === Web3 Provider ===
def get_web3(url: str) -> Web3:
    """Initialize and return a Web3 instance with connection verification."""
    if not url or "YOUR_PROJECT_ID" in url:
        logger.warning("Provider URL not configured properly. Using fallback public provider.")
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        raise ConnectionError(f"Unable to connect to Web3 provider: {url}")
    logger.debug("Web3 connection established successfully.")
    return w3


# === Utility Functions ===
def exponential_backoff(attempt: int, total_wait: float) -> float:
    """Apply exponential backoff with jitter, capped by limits."""
    delay = min(Config.RETRY_BASE_DELAY * (2 ** attempt), Config.MAX_RETRY_DELAY)
    jitter = random.uniform(0.8, 1.2)
    wait_time = min(delay * jitter, Config.MAX_TOTAL_BACKOFF - total_wait)

    if wait_time > 0:
        logger.debug("Retrying in %.2f seconds (attempt %d)...", wait_time, attempt + 1)
        time.sleep(wait_time)
    return total_wait + wait_time


def fetch_gas_prices(web3: Web3, retries: int = Config.RETRY_LIMIT) -> Optional[Dict[str, Any]]:
    """Fetch gas prices and fees from Ethereum network. Returns values in Gwei or None on failure."""
    total_wait = 0.0
    for attempt in range(retries):
        try:
            gas_price_wei = web3.eth.gas_price
            block = web3.eth.get_block("pending")

            base_fee_wei = block.get("baseFeePerGas", 0)
            priority_fee_wei = gas_price_wei - base_fee_wei if base_fee_wei else None

            result = {
                "gas_price_gwei": float(web3.from_wei(gas_price_wei, "gwei")),
                "base_fee_gwei": float(web3.from_wei(base_fee_wei, "gwei")) if base_fee_wei else None,
                "priority_fee_gwei": float(web3.from_wei(priority_fee_wei, "gwei")) if priority_fee_wei else None,
                "block_number": block.get("number"),
            }

            if Config.OUTPUT_JSON:
                print(json.dumps(result, ensure_ascii=False))
            else:
                logger.info(
                    "Gas [Gwei] total=%.2f base=%.2f priority=%.2f (block=%s)",
                    result["gas_price_gwei"],
                    result["base_fee_gwei"] or 0.0,
                    result["priority_fee_gwei"] or 0.0,
                    result["block_number"],
                )
            return result

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as net_err:
            logger.warning("Network issue (attempt %d/%d): %s", attempt + 1, retries, net_err)
        except Exception as err:
            logger.exception("Unexpected error (attempt %d/%d): %s", attempt + 1, retries, err)

        total_wait = exponential_backoff(attempt, total_wait)
        if total_wait >= Config.MAX_TOTAL_BACKOFF:
            break

    logger.error("Failed to fetch gas prices after %d attempts.", retries)
    return None


# === Graceful Shutdown ===
class GracefulKiller:
    """Signal handler for clean shutdown."""
    def __init__(self) -> None:
        self.kill_now = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._handler)

    def _handler(self, signum, frame) -> None:
        logger.info("Termination signal received (%s). Exiting gracefully...", signum)
        self.kill_now = True


# === Main Monitor ===
def monitor_gas_prices(interval: int, killer: GracefulKiller) -> None:
    """Main monitoring loop."""
    logger.info("Gas price monitor started (interval=%ds)", interval)
    web3 = None

    while not killer.kill_now:
        try:
            if web3 is None:
                web3 = get_web3(Config.PROVIDER_URL)
                logger.info("Connected to provider: %s", Config.PROVIDER_URL)

            if fetch_gas_prices(web3) is None:
                logger.warning("No gas price data retrieved in this cycle.")
        except Exception as e:
            logger.exception("Error during monitoring cycle: %s", e)
            web3 = None  # reconnect next iteration

        for _ in range(interval):
            if killer.kill_now:
                break
            time.sleep(1)

    logger.info("Gas price monitoring stopped.")


# === Entry Point ===
def main() -> None:
    killer = GracefulKiller()
    atexit.register(lambda: logger.info("Process exited cleanly."))
    try:
        monitor_gas_prices(Config.MONITOR_INTERVAL, killer)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as e:
        logger.exception("Fatal error: %s", e)


if __name__ == "__main__":
    main()
