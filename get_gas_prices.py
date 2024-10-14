import time
import logging
from web3 import Web3
from web3.exceptions import TimeExhausted
import requests
import sys

# Infura or another provider URL for Base network
INFURA_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

# Initialize Web3 with a 10-second timeout
web3 = Web3(Web3.HTTPProvider(INFURA_URL, request_kwargs={'timeout': 10}))

def get_gas_prices(retries=5, delay=1):
    """
    Fetches and logs the current gas prices in gwei with retry logic.
    
    Args:
    retries (int): Number of retries for fetching data.
    delay (int): Initial delay between retries (exponential backoff applied).
    """
    attempt = 0

    while attempt < retries:
        try:
            gas_price = web3.eth.gas_price  # Get the current gas price in wei
            pending_block = web3.eth.get_block('pending')  # Fetch the pending block

            if 'baseFeePerGas' not in pending_block:
                logging.warning("Pending block does not contain 'baseFeePerGas'. Using only gas price.")
                gas_price_gwei = web3.from_wei(gas_price, 'gwei')
                logging.info(f"Gas Price: {gas_price_gwei} gwei (no base fee available)")
                return

            base_fee = pending_block['baseFeePerGas']  # Base fee from the pending block in wei
            priority_fee = gas_price - base_fee  # Priority fee (MaxFeePerGas - BaseFeePerGas) in wei

            # Convert from wei to gwei for readability
            gas_price_gwei = web3.from_wei(gas_price, 'gwei')
            base_fee_gwei = web3.from_wei(base_fee, 'gwei')
            priority_fee_gwei = web3.from_wei(priority_fee, 'gwei')

            logging.info(f"Gas Price: {gas_price_gwei} gwei | Base Fee: {base_fee_gwei} gwei | Priority Fee: {priority_fee_gwei} gwei")
            return

        except requests.exceptions.Timeout as e:
            logging.error(f"Request timed out: {e}. Retrying... {attempt+1}/{retries}")
        except TimeExhausted as e:
            logging.error(f"Web3 provider took too long to respond: {e}. Retrying... {attempt+1}/{retries}")
        except Exception as e:
            logging.error(f"Unexpected error: {e}. Retrying... {attempt+1}/{retries}")
        
        attempt += 1
        time.sleep(delay * 2 ** attempt)  # Exponential backoff

    logging.error(f"Failed to fetch gas prices after {retries} attempts.")
    sys.exit(1)

def main(interval=10, retries=5, delay=1):
    """
    Main loop to fetch gas prices at the specified interval.
    
    Args:
    interval (int): Time interval (in seconds) between fetching gas prices.
    retries (int): Number of retries for fetching data in case of failures.
    delay (int): Initial delay between retries (applies exponential backoff).
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
    # Configure logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    # Run the main function with the desired interval (in seconds)
    main(interval=10)
