import time
import logging
from web3 import Web3
from web3.exceptions import TimeExhausted
import requests
import sys
from typing import Optional, Dict

# Configuration
PROVIDER_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"
LOG_FILE = "gas_price_monitor.log"
RETRY_LIMIT = 5
RETRY_DELAY = 1

# Initialize Web3 with a timeout
web3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))


def fetch_gas_prices(retries: int = RETRY_LIMIT, delay: int = RETRY_DELAY) -> Optional[Dict[str, float]]:
    """
    Fetch current gas prices in gwei with retries and exponential backoff.

    Args:
        retries (int): Number of retry attempts.
        delay (int): Initial delay between retries (in seconds).

    Returns:
        dict or None: Dictionary containing gas prices (in gwei), or None on failure.
    """
    for attempt in range(retries):
        try:
            gas_price = web3.eth.gas_price
            pending_block = web3.eth.get_block('pending')

            # Validate presence of 'baseFeePerGas'
            if 'baseFeePerGas' not in pending_block:
                logging.warning("Pending block lacks 'baseFeePerGas'. Returning only the gas price.")
                return {"gas_price": web3.from_wei(gas_price, 'gwei')}

            base_fee = pending_block['baseFeePerGas']
            priority_fee = gas_price - base_fee

            gas_data = {
                "gas_price": web3.from_wei(gas_price, 'gwei'),
                "base_fee": web3.from_wei(base_fee, 'gwei'),
                "priority_fee": web3.from_wei(priority_fee, 'gwei'),
            }

            logging.info(
                "Gas Price: %.2f gwei | Base Fee: %.2f gwei | Priority Fee: %.2f gwei",
                gas_data["gas_price"], gas_data["base_fee"], gas_data["priority_fee"]
            )
            return gas_data

        except (requests.exceptions.Timeout, TimeExhausted) as e:
            logging.warning(f"Timeout or connection issue: {e}. Retrying {attempt + 1}/{retries}...")
        except Exception as e:
            logging.error(f"Unexpected error: {e}. Retrying {attempt + 1}/{retries}...")

        time.sleep(delay * 2 ** attempt)  # Exponential backoff

    logging.error("Failed to fetch gas prices after all retries.")
    return None


def monitor_gas_prices(interval: int = 10, retries: int = RETRY_LIMIT, delay: int = RETRY_DELAY):
    """
    Monitor and log gas prices at a specified interval.

    Args:
        interval (int): Interval (in seconds) between gas price fetches.
        retries (int): Number of retry attempts.
        delay (int): Initial delay between retries (exponential backoff applied).
    """
    logging.info("Starting gas price monitoring...")

    try:
        while True:
            gas_prices = fetch_gas_prices(retries, delay)
            if gas_prices:
                logging.info("Fetched gas prices: %s", gas_prices)
            else:
                logging.warning("Failed to fetch gas prices in this cycle.")
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Monitoring stopped by user.")
    except Exception as e:
        logging.error(f"Unexpected error during monitoring: {e}")
    finally:
        logging.info("Gas price monitoring has been terminated.")


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE),
        ],
    )

    # Start monitoring gas prices
    monitor_gas_prices(interval=10)
