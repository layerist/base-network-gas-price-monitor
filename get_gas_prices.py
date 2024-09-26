import time
import logging
from web3 import Web3

# Infura or another provider URL for Base network
INFURA_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

# Initialize Web3
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

def get_gas_prices():
    """Fetches and logs the current gas prices in gwei."""
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

    except Exception as e:
        logging.error(f"An error occurred while fetching gas prices: {e}")

def main(interval=10):
    """Main loop to fetch gas prices at the specified interval."""
    logging.info("Starting gas price monitoring...")
    
    try:
        while True:
            get_gas_prices()
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
