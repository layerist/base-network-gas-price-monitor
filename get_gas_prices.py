import os
import time
import logging
import atexit
import signal
from typing import Optional, Dict

from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException

# === Configuration ===
PROVIDER_URL = os.getenv("PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID")
LOG_FILE = "gas_price_monitor.log"
RETRY_LIMIT = 5
RETRY_DELAY = 1  # seconds
MAX_RETRY_DELAY = 30  # cap delay
MONITOR_INTERVAL = 10  # seconds

# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode='a'),
    ],
)

# === Web3 Setup ===
web3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))
if not web3.is_connected():
    logging.warning("Web3 provider not connected. Check your PROVIDER_URL.")

# === Utility Functions ===
def exponential_backoff(attempt: int, base_delay: int, max_delay: int) -> None:
    delay = min(base_delay * (2 ** attempt), max_delay)
    logging.debug("Sleeping for %.2f seconds before retry...", delay)
    time.sleep(delay)

def fetch_gas_prices(retries: int = RETRY_LIMIT, delay: int = RETRY_DELAY) -> Optional[Dict[str, Optional[float]]]:
    """
    Fetch current gas prices with retries and exponential backoff.
    Returns gas price dict in Gwei or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price = web3.eth.gas_price
            pending_block = web3.eth.get_block('pending')

            base_fee = pending_block.get('baseFeePerGas', 0)
            priority_fee = gas_price - base_fee if base_fee else None

            gas_data = {
                "gas_price": float(web3.from_wei(gas_price, 'gwei')),
                "base_fee": float(web3.from_wei(base_fee, 'gwei')) if base_fee else None,
                "priority_fee": float(web3.from_wei(priority_fee, 'gwei')) if priority_fee else None,
            }

            logging.info("Gas Prices: %s", gas_data)
            return gas_data

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logging.warning("Network-related error: %s [Attempt %d/%d]", e, attempt + 1, retries)
        except Exception as e:
            logging.error("Unexpected error: %s [Attempt %d/%d]", e, attempt + 1, retries)

        exponential_backoff(attempt, delay, MAX_RETRY_DELAY)

    logging.error("Failed to fetch gas prices after %d retries.", retries)
    return None

# === Main Monitor Loop ===
def monitor_gas_prices(interval: int = MONITOR_INTERVAL) -> None:
    """Continuously monitors and logs gas prices."""
    logging.info("Started gas price monitoring every %ds.", interval)
    while True:
        success = fetch_gas_prices()
        if not success:
            logging.warning("Gas price fetch failed this cycle.")
        time.sleep(interval)

# === Graceful Shutdown ===
def cleanup() -> None:
    logging.info("Shutting down gracefully...")

def handle_exit(signum, frame):
    logging.info("Received termination signal (%s). Exiting...", signum)
    exit(0)

atexit.register(cleanup)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)

# === Entry Point ===
if __name__ == "__main__":
    monitor_gas_prices()
