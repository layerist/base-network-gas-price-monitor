import time
from web3 import Web3

# Infura or another provider URL for Base network
INFURA_URL = "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"

# Initialize Web3
web3 = Web3(Web3.HTTPProvider(INFURA_URL))

def get_gas_prices():
    """Fetches and prints the current gas prices in gwei."""
    try:
        gas_price = web3.eth.gas_price  # Get the current gas price
        base_fee = web3.eth.get_block('pending')['baseFeePerGas']  # Get the base fee from the pending block
        priority_fee = gas_price - base_fee  # Priority fee (MaxFeePerGas - BaseFeePerGas)

        # Convert from wei to gwei for readability
        gas_price_gwei = web3.from_wei(gas_price, 'gwei')
        base_fee_gwei = web3.from_wei(base_fee, 'gwei')
        priority_fee_gwei = web3.from_wei(priority_fee, 'gwei')

        print(f"Gas Price: {gas_price_gwei} gwei | Base Fee: {base_fee_gwei} gwei | Priority Fee: {priority_fee_gwei} gwei")

    except Exception as e:
        print(f"An error occurred: {e}")

def main():
    """Main loop to fetch gas prices every second."""
    print("Starting gas price monitoring...")
    try:
        while True:
            get_gas_prices()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nScript stopped by user.")

if __name__ == "__main__":
    main()
