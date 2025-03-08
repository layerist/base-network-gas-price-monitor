import os
import time
import logging
import atexit
from web3 import Web3
from web3.exceptions import TimeExhausted, ProviderConnectionError
from requests.exceptions import Timeout, RequestException
from typing import Optional, Dict

# Configuration
PROVIDER_URL = os.getenv("PROVIDER_URL", "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID")
LOG_FILE = "gas_price_monitor.log"
RETRY_LIMIT = 5
RETRY_DELAY = 1
MONITOR_INTERVAL = 10  # Interval between gas price fetches (seconds)

# Initialize Web3
web3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode='a'),
    ],
)

def exponential_backoff(attempt: int, base_delay: int) -> None:
    """Waits for an exponentially increasing time period."""
    time.sleep(base_delay * (2 ** attempt))

def fetch_gas_prices(retries: int = RETRY_LIMIT, delay: int = RETRY_DELAY) -> Optional[Dict[str, float]]:
    """
    Fetches current gas prices in gwei with retries and exponential backoff.
    
    Returns:
        dict or None: Gas price details or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price = web3.eth.gas_price
            pending_block = web3.eth.get_block('pending')

            base_fee = pending_block.get('baseFeePerGas')
            priority_fee = gas_price - base_fee if base_fee else None

            gas_data = {
                "gas_price": web3.from_wei(gas_price, 'gwei'),
                "base_fee": web3.from_wei(base_fee, 'gwei') if base_fee else None,
                "priority_fee": web3.from_wei(priority_fee, 'gwei') if priority_fee else None,
            }

            logging.info("Gas Prices: %s", gas_data)
            return gas_data

        except (Timeout, TimeExhausted, ProviderConnectionError, RequestException) as e:
            logging.warning("Network error: %s. Retrying %d/%d...", e, attempt + 1, retries)
        except Exception as e:
            logging.error("Unexpected error: %s. Retrying %d/%d...", e, attempt + 1, retries)

        exponential_backoff(attempt, delay)

    logging.error("Failed to fetch gas prices after all retries.")
    return None

def monitor_gas_prices(interval: int = MONITOR_INTERVAL):
    """Monitors and logs gas prices at a specified interval."""
    logging.info("Starting gas price monitoring...")
    try:
        while True:
            gas_prices = fetch_gas_prices()
            if not gas_prices:
                logging.warning("Failed to fetch gas prices in this cycle.")
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    except Exception as e:
        logging.error("Unexpected error during monitoring: %s", e)
    finally:
        logging.info("Gas price monitoring has been terminated.")

def cleanup():
    """Executes cleanup actions before exiting."""
    logging.info("Shutting down gracefully.")

atexit.register(cleanup)

if __name__ == "__main__":
    monitor_gas_prices()
