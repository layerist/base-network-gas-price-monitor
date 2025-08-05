import os
import time
import logging
import atexit
import signal
import random
from typing import Optional, Dict

from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException

# === Configuration ===
class Config:
    PROVIDER_URL: str = os.getenv("PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID")
    LOG_FILE: str = "gas_price_monitor.log"
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT: int = 5
    RETRY_BASE_DELAY: int = 1        # seconds
    MAX_RETRY_DELAY: int = 30        # seconds
    MONITOR_INTERVAL: int = 10       # seconds


# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

for output in (logging.StreamHandler(), logging.FileHandler(Config.LOG_FILE, mode="a")):
    output.setFormatter(formatter)
    logger.addHandler(output)

# === Global Control Flag ===
running = True

# === Web3 Setup ===
def get_web3(url: str) -> Web3:
    """Initialize a Web3 instance with basic connectivity check."""
    try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            raise ConnectionError("Web3 provider is not connected. Check your PROVIDER_URL.")
        return w3
    except Exception as e:
        logger.critical("Failed to connect to Web3 provider: %s", e)
        raise

web3 = get_web3(Config.PROVIDER_URL)


# === Utility Functions ===
def exponential_backoff(attempt: int) -> None:
    """Wait for an exponentially increasing backoff time with jitter."""
    delay = min(Config.RETRY_BASE_DELAY * (2 ** attempt), Config.MAX_RETRY_DELAY)
    jitter = random.uniform(0.8, 1.2)
    time.sleep(delay * jitter)
    logger.debug("Retrying after %.2f seconds...", delay * jitter)


def fetch_gas_prices(retries: int = Config.RETRY_LIMIT) -> Optional[Dict[str, Optional[float]]]:
    """
    Attempt to fetch gas price, base fee, and priority fee from the Ethereum network.

    Returns:
        Dictionary with 'gas_price', 'base_fee', and 'priority_fee' in Gwei, or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price_wei = web3.eth.gas_price
            block = web3.eth.get_block("pending")

            base_fee_wei = block.get("baseFeePerGas", 0)
            priority_fee_wei = gas_price_wei - base_fee_wei if base_fee_wei else None

            gas_data = {
                "gas_price": float(web3.from_wei(gas_price_wei, "gwei")),
                "base_fee": float(web3.from_wei(base_fee_wei, "gwei")) if base_fee_wei else None,
                "priority_fee": float(web3.from_wei(priority_fee_wei, "gwei")) if priority_fee_wei else None
            }

            logger.info(
                "Gas Prices [Gwei] — Total: %.2f | Base: %.2f | Priority: %.2f",
                gas_data["gas_price"],
                gas_data["base_fee"] or 0.0,
                gas_data["priority_fee"] or 0.0
            )

            return gas_data

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as net_err:
            logger.warning("Network error (attempt %d/%d): %s", attempt + 1, retries, net_err)
        except Exception as err:
            logger.exception("Unexpected error (attempt %d/%d): %s", attempt + 1, retries, err)

        exponential_backoff(attempt)

    logger.error("All %d attempts failed. Could not retrieve gas prices.", retries)
    return None


def monitor_gas_prices(interval: int = Config.MONITOR_INTERVAL) -> None:
    """Main monitoring loop that logs gas prices at regular intervals."""
    logger.info("Starting gas price monitor (every %d seconds)...", interval)
    while running:
        try:
            if fetch_gas_prices() is None:
                logger.warning("Failed to retrieve gas prices this cycle.")
        except Exception as e:
            logger.exception("Unhandled error in monitoring loop: %s", e)
        time.sleep(interval)


# === Cleanup and Signal Handling ===
def cleanup() -> None:
    logger.info("Gas price monitor stopped.")

def handle_exit(signum, frame) -> None:
    global running
    logger.info("Received termination signal (%s). Shutting down...", signum)
    running = False

atexit.register(cleanup)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# === Entry Point ===
if __name__ == "__main__":
    try:
        monitor_gas_prices()
    except KeyboardInterrupt:
        logger.info("Manual interruption received. Exiting...")
