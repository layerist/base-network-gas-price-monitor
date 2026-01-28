#!/usr/bin/env python3
"""
Robust EVM gas price monitor (Base by default).

Improvements:
- Provider health scoring + cooldown
- Unified retry with exponential backoff + jitter
- Correct EIP-1559 fee derivation
- Deterministic shutdown (no busy loops)
- Structured logging (human or JSON)
"""

from __future__ import annotations

import os
import time
import json
import random
import signal
import atexit
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Callable, Type
from functools import wraps

from logging.handlers import RotatingFileHandler
from web3 import Web3
from web3.exceptions import ProviderConnectionError, TimeExhausted
from requests.exceptions import Timeout, RequestException


# ============================================================
# CONFIG
# ============================================================

@dataclass(frozen=True)
class Config:
    PROVIDER_URL: str = os.getenv(
        "PROVIDER_URL",
        "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID",
    ).strip()

    FALLBACK_PROVIDERS: tuple[str, ...] = (
        "https://base.llamarpc.com",
        "https://base-mainnet.public.blastapi.io",
    )

    LOG_FILE: str = os.getenv("LOG_FILE", "gas_monitor.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    OUTPUT_JSON: bool = os.getenv("OUTPUT_JSON", "false").lower() == "true"
    COLOR_LOGS: bool = os.getenv("COLOR_LOGS", "true").lower() == "true"

    HTTP_TIMEOUT: int = int(os.getenv("HTTP_TIMEOUT", 10))

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", 1.0))
    MAX_RETRY_DELAY: float = float(os.getenv("MAX_RETRY_DELAY", 30.0))

    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 10))
    PROVIDER_COOLDOWN: int = int(os.getenv("PROVIDER_COOLDOWN", 60))


CFG = Config()


# ============================================================
# LOGGING
# ============================================================

class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[94m",
        "INFO": "\033[92m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if CFG.COLOR_LOGS and record.levelname in self.COLORS:
            return f"{self.COLORS[record.levelname]}{msg}{self.RESET}"
        return msg


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("GasMonitor")
    logger.setLevel(getattr(logging, CFG.LOG_LEVEL, logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = ColorFormatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        CFG.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


logger = setup_logger()


# ============================================================
# RETRY DECORATOR
# ============================================================

RETRY_ERRORS: tuple[Type[Exception], ...] = (
    Timeout,
    TimeExhausted,
    ProviderConnectionError,
    RequestException,
)


def retry(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        delay = CFG.RETRY_BASE_DELAY

        for attempt in range(1, CFG.RETRY_LIMIT + 1):
            try:
                return fn(*args, **kwargs)
            except RETRY_ERRORS as e:
                if attempt == CFG.RETRY_LIMIT:
                    raise
                jitter = random.uniform(0.8, 1.2)
                sleep_time = min(delay * jitter, CFG.MAX_RETRY_DELAY)
                logger.warning(
                    "Retry %d/%d in %.2fs (%s)",
                    attempt,
                    CFG.RETRY_LIMIT,
                    sleep_time,
                    e,
                )
                time.sleep(sleep_time)
                delay *= 2

    return wrapper


# ============================================================
# WEB3 CLIENT WITH HEALTH
# ============================================================

class Provider:
    def __init__(self, url: str):
        self.url = url
        self.cooldown_until = 0.0

    def available(self) -> bool:
        return time.time() >= self.cooldown_until

    def penalize(self) -> None:
        self.cooldown_until = time.time() + CFG.PROVIDER_COOLDOWN


class Web3Client:
    def __init__(self, primary: str, fallbacks: List[str]):
        self.providers = [Provider(primary), *map(Provider, fallbacks)]
        self.web3: Optional[Web3] = None

    def _connect(self) -> None:
        for p in self.providers:
            if not p.available():
                continue
            try:
                logger.info("Connecting to %s", p.url)
                w3 = Web3(Web3.HTTPProvider(p.url, request_kwargs={"timeout": CFG.HTTP_TIMEOUT}))
                if w3.is_connected():
                    self.web3 = w3
                    return
            except Exception:
                p.penalize()
        raise ConnectionError("No healthy providers available")

    def get(self) -> Web3:
        if not self.web3 or not self.web3.is_connected():
            self._connect()
        return self.web3

    def penalize_current(self) -> None:
        if self.web3:
            for p in self.providers:
                if p.url in self.web3.provider.endpoint_uri:
                    p.penalize()
        self.web3 = None


# ============================================================
# CORE
# ============================================================

@retry
def fetch_gas(client: Web3Client) -> Dict[str, Any]:
    w3 = client.get()

    block = w3.eth.get_block("pending")
    base_fee = block.get("baseFeePerGas", 0)

    try:
        priority_fee = w3.eth.max_priority_fee
    except Exception:
        priority_fee = w3.eth.gas_price - base_fee

    max_fee = base_fee + priority_fee

    return {
        "gas_price_gwei": float(w3.from_wei(max_fee, "gwei")),
        "base_fee_gwei": float(w3.from_wei(base_fee, "gwei")),
        "priority_fee_gwei": float(w3.from_wei(priority_fee, "gwei")),
        "block": block.get("number"),
        "timestamp": int(time.time()),
    }


def emit(data: Dict[str, Any]) -> None:
    if CFG.OUTPUT_JSON:
        print(json.dumps(data, ensure_ascii=False))
    else:
        logger.info(
            "Gas %.2f gwei | base %.2f | tip %.2f | block %s",
            data["gas_price_gwei"],
            data["base_fee_gwei"],
            data["priority_fee_gwei"],
            data["block"],
        )


# ============================================================
# SHUTDOWN
# ============================================================

class GracefulShutdown:
    stop = False

    def __init__(self) -> None:
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_: Any) -> None:
        logger.info("Shutdown requested")
        self.stop = True


# ============================================================
# MAIN
# ============================================================

def monitor() -> None:
    shutdown = GracefulShutdown()
    client = Web3Client(CFG.PROVIDER_URL, list(CFG.FALLBACK_PROVIDERS))

    logger.info("Gas monitor started")

    while not shutdown.stop:
        try:
            data = fetch_gas(client)
            emit(data)
        except Exception as e:
            logger.error("Fetch failed: %s", e)
            client.penalize_current()

        shutdown.stop or time.sleep(CFG.MONITOR_INTERVAL)

    logger.info("Gas monitor stopped")


def main() -> None:
    atexit.register(lambda: logger.info("Process exiting"))
    monitor()


if __name__ == "__main__":
    main()
