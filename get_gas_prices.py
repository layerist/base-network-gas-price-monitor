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
RETRY_LIMIT = 5
RETRY_BASE_DELAY = 1  # seconds
MAX_RETRY_DELAY = 30
MONITOR_INTERVAL = 10  # seconds

# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
file_handler = logging.FileHandler(LOG_FILE, mode="a")
file_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

# === Web3 Setup ===
web3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))
if not web3.is_connected():
    logger.warning("Web3 provider not connected. Check your PROVIDER_URL.")

# === Control Flag ===
running = True

def exponential_backoff(attempt: int, base_delay: float, max_delay: float) -> None:
    """Sleep using exponential backoff with jitter."""
    delay = min(base_delay * (2 ** attempt), max_delay)
    delay *= random.uniform(0.8, 1.2)  # Add jitter
    logger.debug("Sleeping %.2f seconds before retry...", delay)
    time.sleep(delay)

def fetch_gas_prices(retries: int = RETRY_LIMIT) -> Optional[Dict[str, Any]]:
    """
    Fetch current gas prices with retries and exponential backoff.
    Returns gas price data in Gwei or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price = web3.eth.gas_price

            try:
                pending_block = web3.eth.get_block('pending')
                base_fee = pending_block.get('baseFeePerGas', 0)
            except Exception:
                base_fee = 0

            priority_fee = gas_price - base_fee if base_fee else None

            gas_data = {
                "gas_price": float(web3.from_wei(gas_price, 'gwei')),
                "base_fee": float(web3.from_wei(base_fee, 'gwei')) if base_fee else None,
                "priority_fee": float(web3.from_wei(priority_fee, 'gwei')) if priority_fee else None,
            }

            logger.info("Gas Prices: %s", gas_data)
            return gas_data

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logger.warning("Network error: %s [Attempt %d/%d]", e, attempt + 1, retries)
        except Exception as e:
            logger.exception("Unexpected error: %s [Attempt %d/%d]", e, attempt + 1, retries)

        exponential_backoff(attempt, RETRY_BASE_DELAY, MAX_RETRY_DELAY)

    logger.error("Failed to fetch gas prices after %d retries.", retries)
    return None

def monitor_gas_prices(interval: int = MONITOR_INTERVAL) -> None:
    """Continuously monitor and log gas prices until stopped."""
    logger.info("Starting gas price monitoring every %d seconds.", interval)
    while running:
        try:
            result = fetch_gas_prices()
            if result is None:
                logger.warning("Gas price fetch failed this cycle.")
        except Exception as e:
            logger.exception("Unhandled error in monitor loop: %s", e)
        time.sleep(interval)

def cleanup() -> None:
    logger.info("Shutting down gracefully...")

def handle_exit(signum, frame) -> None:
    global running
    logger.info("Received termination signal (%s). Exiting...", signum)
    running = False

atexit.register(cleanup)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

if __name__ == "__main__":
    try:
        monitor_gas_prices()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Exiting...")
