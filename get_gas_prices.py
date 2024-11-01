import time
import logging
from web3 import Web3
from web3.exceptions import TimeExhausted
import requests
import sys
from typing import Optional

# Infura or another provider URL for the Base network
INFURA_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

# Initialize Web3 with a 10-second timeout
web3 = Web3(Web3.HTTPProvider(INFURA_URL, request_kwargs={'timeout': 10}))

def get_gas_prices(retries: int = 5, delay: int = 1) -> Optional[dict]:
    """
    Fetches current gas prices in gwei with retry logic.

    Args:
        retries (int): Number of retries for fetching data.
        delay (int): Initial delay between retries (with exponential backoff).
    
    Returns:
        dict or None: Gas prices in gwei if successful, None otherwise.
    """
    attempt = 0

    while attempt < retries:
        try:
            gas_price = web3.eth.gas_price
            pending_block = web3.eth.get_block('pending')

            if 'baseFeePerGas' not in pending_block:
                logging.warning("Pending block lacks 'baseFeePerGas'. Using only gas price.")
                gas_price_gwei = web3.from_wei(gas_price, 'gwei')
                logging.info(f"Gas Price: {gas_price_gwei} gwei (no base fee available)")
                return {"gas_price": gas_price_gwei}

            base_fee = pending_block['baseFeePerGas']
            priority_fee = gas_price - base_fee

            # Convert from wei to gwei for readability
            gas_data = {
                "gas_price": web3.from_wei(gas_price, 'gwei'),
                "base_fee": web3.from_wei(base_fee, 'gwei'),
                "priority_fee": web3.from_wei(priority_fee, 'gwei')
            }

            logging.info(f"Gas Price: {gas_data['gas_price']} gwei | Base Fee: {gas_data['base_fee']} gwei | Priority Fee: {gas_data['priority_fee']} gwei")
            return gas_data

        except (requests.exceptions.Timeout, TimeExhausted) as e:
            logging.error(f"Connection issue ({e}). Retrying... {attempt + 1}/{retries}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}. Retrying... {attempt + 1}/{retries}")
        
        attempt += 1
        time.sleep(delay * 2 ** attempt)  # Exponential backoff

    logging.error(f"Failed to fetch gas prices after {retries} attempts.")
    return None

def main(interval: int = 10, retries: int = 5, delay: int = 1):
    """
    Main loop to fetch gas prices at a specified interval.
    
    Args:
        interval (int): Time interval (in seconds) between gas price fetches.
        retries (int): Number of retries for fetching data on failure.
        delay (int): Initial delay between retries (exponential backoff applied).
    """
    logging.info("Starting gas price monitoring...")

    try:
        while True:
            get_gas_prices(retries, delay)
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Script stopped by user.")
    except Exception as e:
        logging.error(f"Unexpected error in main loop: {e}")
    finally:
        logging.info("Gas price monitoring stopped.")

if __name__ == "__main__":
    # Configure logging with more detailed format
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )

    # Run the main function with the desired interval (in seconds)
    main(interval=10)
