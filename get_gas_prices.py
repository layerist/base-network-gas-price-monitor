import os
import time
import logging
import random
import signal
import atexit
from typing import Optional, Dict, Any

from logging.handlers import RotatingFileHandler
from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException


# === Configuration ===
class Config:
    PROVIDER_URL: str = os.getenv(
        "PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"
    )
    LOG_FILE: str = "gas_price_monitor.log"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT: int = 5
    RETRY_BASE_DELAY: int = 1        # seconds
    MAX_RETRY_DELAY: int = 30        # seconds
    MAX_TOTAL_BACKOFF: int = 120     # safety cap (seconds)
    MONITOR_INTERVAL: int = 10       # seconds


# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] %(name)s | %(message)s'
)

# Rotate logs: max 5MB, keep 3 backups
file_handler = RotatingFileHandler(Config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
file_handler.setFormatter(formatter)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)


# === Web3 Provider ===
def get_web3(url: str) -> Web3:
    """Initialize and return a Web3 instance with connectivity check."""
    w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
    if not w3.is_connected():
        raise ConnectionError(f"Web3 provider not connected: {url}")
    return w3


# === Utility Functions ===
def exponential_backoff(attempt: int, total_wait: float) -> float:
    """
    Sleep with exponential backoff and jitter, capped by max delay and total backoff.
    Returns the new accumulated total wait time.
    """
    delay = min(Config.RETRY_BASE_DELAY * (2 ** attempt), Config.MAX_RETRY_DELAY)
    jitter = random.uniform(0.8, 1.2)
    wait_time = min(delay * jitter, Config.MAX_TOTAL_BACKOFF - total_wait)

    if wait_time > 0:
        logger.debug("Retrying after %.2f seconds...", wait_time)
        time.sleep(wait_time)

    return total_wait + wait_time


def fetch_gas_prices(web3: Web3, retries: int = Config.RETRY_LIMIT) -> Optional[Dict[str, Any]]:
    """
    Fetch gas price, base fee, and priority fee from the Ethereum network.
    Returns dict with values in Gwei or None on failure.
    """
    total_wait = 0.0
    for attempt in range(retries):
        try:
            gas_price_wei = web3.eth.gas_price
            block = web3.eth.get_block("pending")

            base_fee_wei = block.get("baseFeePerGas", 0)
            priority_fee_wei = gas_price_wei - base_fee_wei if base_fee_wei else None

            gas_data = {
                "gas_price": float(web3.from_wei(gas_price_wei, "gwei")),
                "base_fee": float(web3.from_wei(base_fee_wei, "gwei")) if base_fee_wei else None,
                "priority_fee": float(web3.from_wei(priority_fee_wei, "gwei")) if priority_fee_wei else None,
            }

            logger.info(
                "Gas prices [Gwei] | total=%.2f base=%.2f priority=%.2f",
                gas_data["gas_price"],
                gas_data["base_fee"] or 0.0,
                gas_data["priority_fee"] or 0.0,
            )
            return gas_data

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as net_err:
            logger.warning("Network error (attempt %d/%d): %s", attempt + 1, retries, net_err)
        except Exception as err:
            logger.exception("Unexpected error (attempt %d/%d): %s", attempt + 1, retries, err)

        total_wait = exponential_backoff(attempt, total_wait)
        if total_wait >= Config.MAX_TOTAL_BACKOFF:
            break

    logger.error("All %d attempts failed. Could not retrieve gas prices.", retries)
    return None


# === Monitoring ===
class GracefulKiller:
    """Handles SIGINT/SIGTERM for graceful shutdown."""
    def __init__(self) -> None:
        self.kill_now = False
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame) -> None:
        logger.info("Received termination signal (%s). Shutting down...", signum)
        self.kill_now = True


def monitor_gas_prices(interval: int, killer: GracefulKiller) -> None:
    """Monitoring loop that logs gas prices at regular intervals until stopped."""
    logger.info("Starting gas price monitor (every %d seconds)...", interval)

    web3 = None
    while not killer.kill_now:
        if web3 is None:
            try:
                web3 = get_web3(Config.PROVIDER_URL)
                logger.info("Connected to Web3 provider.")
            except Exception as e:
                logger.critical("Failed to connect to Web3 provider: %s", e)
                time.sleep(interval)
                continue

        try:
            if fetch_gas_prices(web3) is None:
                logger.warning("Failed to retrieve gas prices this cycle.")
        except Exception as e:
            logger.exception("Unhandled error in monitoring loop: %s", e)
            web3 = None  # force reconnect on next cycle

        time.sleep(interval)


# === Entry Point ===
def main() -> None:
    killer = GracefulKiller()
    logger.info("Gas price monitor started.")
    try:
        monitor_gas_prices(Config.MONITOR_INTERVAL, killer)
    finally:
        logger.info("Gas price monitor stopped.")


atexit.register(lambda: logger.info("Process exited."))


if __name__ == "__main__":
    main()
