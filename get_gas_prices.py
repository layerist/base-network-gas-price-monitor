import os
import time
import logging
import atexit
import signal
import random
from typing import Optional, Dict, Any

from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException

# === Configuration ===
PROVIDER_URL = os.getenv("PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID")
LOG_FILE = "gas_price_monitor.log"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
RETRY_LIMIT = 5
RETRY_BASE_DELAY = 1  # seconds
MAX_RETRY_DELAY = 30  # seconds
MONITOR_INTERVAL = 10  # seconds

# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

file_handler = logging.FileHandler(LOG_FILE, mode="a")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# === Web3 Setup ===
def get_web3_instance(url: str) -> Web3:
    try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
        if not w3.is_connected():
            raise ConnectionError("Web3 provider not connected. Check your PROVIDER_URL.")
        return w3
    except Exception as e:
        logger.critical("Failed to initialize Web3: %s", e)
        raise

web3 = get_web3_instance(PROVIDER_URL)

# === Control Flag ===
running = True


def exponential_backoff(attempt: int, base_delay: float, max_delay: float) -> None:
    """Sleep with exponential backoff and jitter."""
    delay = min(base_delay * (2 ** attempt), max_delay)
    delay *= random.uniform(0.8, 1.2)
    logger.debug("Sleeping %.2f seconds before retry...", delay)
    time.sleep(delay)


def fetch_gas_prices(retries: int = RETRY_LIMIT) -> Optional[Dict[str, Optional[float]]]:
    """
    Fetch current gas prices with retry logic.
    Returns:
        dict containing gas_price, base_fee, priority_fee (all in Gwei), or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price_wei = web3.eth.gas_price

            try:
                pending_block = web3.eth.get_block('pending')
                base_fee_wei = pending_block.get('baseFeePerGas', 0)
            except Exception as e:
                logger.debug("Failed to get base fee: %s", e)
                base_fee_wei = 0

            priority_fee_wei = gas_price_wei - base_fee_wei if base_fee_wei else None

            result = {
                "gas_price": float(web3.from_wei(gas_price_wei, 'gwei')),
                "base_fee": float(web3.from_wei(base_fee_wei, 'gwei')) if base_fee_wei else None,
                "priority_fee": float(web3.from_wei(priority_fee_wei, 'gwei')) if priority_fee_wei else None,
            }

            logger.info("Gas Prices - Gwei | Total: %.2f | Base: %.2f | Priority: %.2f",
                        result["gas_price"],
                        result["base_fee"] or 0.0,
                        result["priority_fee"] or 0.0)
            return result

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logger.warning("Network error on attempt %d/%d: %s", attempt + 1, retries, e)
        except Exception as e:
            logger.exception("Unexpected error on attempt %d/%d: %s", attempt + 1, retries, e)

        exponential_backoff(attempt, RETRY_BASE_DELAY, MAX_RETRY_DELAY)

    logger.error("Exceeded retry limit (%d). Could not fetch gas prices.", retries)
    return None


def monitor_gas_prices(interval: int = MONITOR_INTERVAL) -> None:
    """Monitor and log gas prices periodically."""
    logger.info("Starting gas price monitoring (every %d seconds)...", interval)
    while running:
        try:
            result = fetch_gas_prices()
            if result is None:
                logger.warning("Gas price fetch failed this cycle.")
        except Exception as e:
            logger.exception("Unhandled error in monitor loop: %s", e)
        time.sleep(interval)


def cleanup() -> None:
    logger.info("Cleanup: shutting down gas price monitor.")


def handle_exit(signum, frame) -> None:
    global running
    logger.info("Exit signal received (%s). Terminating...", signum)
    running = False


# === Signal Handling ===
atexit.register(cleanup)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

# === Main Entry Point ===
if __name__ == "__main__":
    try:
        monitor_gas_prices()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught. Exiting...")
