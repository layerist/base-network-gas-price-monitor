import time
import logging
from web3 import Web3
from web3.exceptions import TimeExhausted
import requests
import sys
from typing import Optional

# Configure provider URL (replace with your actual provider URL/key)
PROVIDER_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

# Initialize Web3 with a timeout
web3 = Web3(Web3.HTTPProvider(PROVIDER_URL, request_kwargs={'timeout': 10}))

def fetch_gas_prices(retries: int = 5, delay: int = 1) -> Optional[dict]:
    """
    Fetch current gas prices in gwei with retry and exponential backoff.

    Args:
        retries (int): Number of retries for fetching data.
        delay (int): Initial delay between retries (in seconds).

    Returns:
        dict or None: Gas prices in gwei if successful, None otherwise.
    """
    for attempt in range(retries):
        try:
            gas_price = web3.eth.gas_price
            pending_block = web3.eth.get_block('pending')

            # Check for 'baseFeePerGas' in the pending block
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
                "Gas Price: %(gas_price).2f gwei | Base Fee: %(base_fee).2f gwei | Priority Fee: %(priority_fee).2f gwei",
                gas_data
            )
            return gas_data

        except (requests.exceptions.Timeout, TimeExhausted) as e:
            logging.warning(f"Connection issue: {e}. Retrying {attempt + 1}/{retries}...")
        except Exception as e:
            logging.error(f"Unexpected error: {e}. Retrying {attempt + 1}/{retries}...")

        time.sleep(delay * 2 ** attempt)  # Exponential backoff

    logging.error("Failed to fetch gas prices after multiple attempts.")
    return None

def monitor_gas_prices(interval: int = 10, retries: int = 5, delay: int = 1):
    """
    Continuously fetch and log gas prices at a specified interval.

    Args:
        interval (int): Time interval (in seconds) between gas price fetches.
        retries (int): Number of retries for fetching data on failure.
        delay (int): Initial delay between retries (exponential backoff applied).
    """
    logging.info("Starting gas price monitoring...")

    try:
        while True:
            gas_prices = fetch_gas_prices(retries, delay)
            if gas_prices:
                logging.info("Gas prices fetched successfully: %s", gas_prices)
            else:
                logging.warning("Failed to fetch gas prices in this cycle.")
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Monitoring interrupted by the user.")
    except Exception as e:
        logging.error(f"Unexpected error during monitoring: {e}")
    finally:
        logging.info("Gas price monitoring stopped.")

if __name__ == "__main__":
    # Set up logging to stdout and file with detailed format
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("gas_price_monitor.log"),
        ],
    )

    # Start the monitoring script
    monitor_gas_prices(interval=10)
