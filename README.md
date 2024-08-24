# Base Network Gas Price Monitor

This Python script monitors gas prices on the Base network every second, displaying the current gas price, base fee, and priority fee in gwei.

## Features

- Fetches gas prices from the Base network every second.
- Displays gas price, base fee, and priority fee in gwei.
- Can be interrupted and stopped with `Ctrl+C`.

## Prerequisites

- Python 3.x
- `web3.py` library

## Installation

1. **Clone the repository:**
    ```bash
    git clone https://github.com/layerist/base-gas-price-monitor.git
    cd base-gas-price-monitor
    ```

2. **Install dependencies:**
    ```bash
    pip install web3
    ```

3. **Set up Infura (or other provider):**
    - Sign up for an [Infura](https://infura.io/) account (or use another provider).
    - Replace `YOUR_PROJECT_ID` in the script with your Infura project ID.

## Usage

Run the script using Python:

```bash
python get_gas_prices.py
```

The script will print the current gas prices in gwei every second.

### Example Output

```
Gas Price: 5.0 gwei | Base Fee: 4.5 gwei | Priority Fee: 0.5 gwei
Gas Price: 5.1 gwei | Base Fee: 4.6 gwei | Priority Fee: 0.5 gwei
...
```

## Stopping the Script

To stop the script, press `Ctrl+C`. This will safely terminate the monitoring loop.

## Contributing

If you'd like to contribute, please fork the repository and make your changes. Pull requests are welcome!

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

### Notes
- Ensure you replace `YOUR_PROJECT_ID` with your actual Infura project ID or the appropriate Base network endpoint.
- You may need to customize the provider URL if you are using a different provider than Infura. 

This script continuously monitors gas prices on the Base network and can be easily extended or modified to suit specific needs.
