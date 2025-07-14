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
    PROVIDER_URL = os.getenv("PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID")
    LOG_FILE = "gas_price_monitor.log"
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

    RETRY_LIMIT = 5
    RETRY_BASE_DELAY = 1  # seconds
    MAX_RETRY_DELAY = 30  # seconds
    MONITOR_INTERVAL = 10  # seconds


# === Logging Setup ===
logger = logging.getLogger("GasPriceMonitor")
logger.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

for handler in [logging.StreamHandler(), logging.FileHandler(Config.LOG_FILE, mode="a")]:
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# === Global Control Flag ===
running = True


# === Web3 Setup ===
def get_web3_instance(url: str) -> Web3:
    try:
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={'timeout': 10}))
        if not w3.is_connected():
            raise ConnectionError("Web3 provider is not connected. Check the PROVIDER_URL.")
        return w3
    except Exception as e:
        logger.critical("Failed to initialize Web3: %s", e)
        raise


web3 = get_web3_instance(Config.PROVIDER_URL)


# === Utility Functions ===
def exponential_backoff(attempt: int) -> None:
    """Wait for an exponentially increasing time with jitter."""
    delay = min(Config.RETRY_BASE_DELAY * (2 ** attempt), Config.MAX_RETRY_DELAY)
    delay *= random.uniform(0.8, 1.2)
    logger.debug("Sleeping for %.2f seconds before retry...", delay)
    time.sleep(delay)


def fetch_gas_prices(retries: int = Config.RETRY_LIMIT) -> Optional[Dict[str, Optional[float]]]:
    """
    Fetch the current gas prices with retry logic.
    Returns:
        A dictionary with gas_price, base_fee, priority_fee in Gwei, or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price_wei = web3.eth.gas_price

            try:
                pending_block = web3.eth.get_block("pending")
                base_fee_wei = pending_block.get("baseFeePerGas", 0)
            except Exception as e:
                logger.debug("Could not retrieve base fee: %s", e)
                base_fee_wei = 0

            priority_fee_wei = gas_price_wei - base_fee_wei if base_fee_wei else None

            result = {
                "gas_price": float(web3.from_wei(gas_price_wei, "gwei")),
                "base_fee": float(web3.from_wei(base_fee_wei, "gwei")) if base_fee_wei else None,
                "priority_fee": float(web3.from_wei(priority_fee_wei, "gwei")) if priority_fee_wei else None,
            }

            logger.info("Gas Prices [Gwei] | Total: %.2f | Base: %.2f | Priority: %.2f",
                        result["gas_price"],
                        result["base_fee"] or 0.0,
                        result["priority_fee"] or 0.0)
            return result

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logger.warning("Network error (attempt %d/%d): %s", attempt + 1, retries, e)
        except Exception as e:
            logger.exception("Unexpected error (attempt %d/%d): %s", attempt + 1, retries, e)

        exponential_backoff(attempt)

    logger.error("All %d attempts failed. Could not retrieve gas prices.", retries)
    return None


def monitor_gas_prices(interval: int = Config.MONITOR_INTERVAL) -> None:
    """Continuously monitor and log gas prices."""
    logger.info("Starting gas price monitoring every %d seconds...", interval)
    while running:
        try:
            if fetch_gas_prices() is None:
                logger.warning("Gas price retrieval failed this cycle.")
        except Exception as e:
            logger.exception("Unhandled error in monitoring loop: %s", e)
        time.sleep(interval)


# === Cleanup and Signal Handling ===
def cleanup() -> None:
    logger.info("Shutting down gas price monitor.")


def handle_exit(signum, frame) -> None:
    global running
    logger.info("Received exit signal (%s). Terminating gracefully...", signum)
    running = False


atexit.register(cleanup)
signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# === Entry Point ===
if __name__ == "__main__":
    try:
        monitor_gas_prices()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Exiting...")
