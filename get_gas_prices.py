#!/usr/bin/env python3
"""
Production-grade EVM Gas Price Monitor

Key improvements:
- HTTP connection pooling
- Weighted provider selection
- Provider health decay / recovery
- Retry with provider reset
- Strict EIP-1559 fee derivation
- Deterministic shutdown
"""

from __future__ import annotations

import os
import time
import json
import random
import signal
import logging
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any, List, Callable, Type
from functools import wraps
from logging.handlers import RotatingFileHandler

import requests
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

    HTTP_TIMEOUT: int = int(os.getenv("HTTP_TIMEOUT", 10))

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", 1))
    RETRY_MAX_DELAY: float = float(os.getenv("RETRY_MAX_DELAY", 30))

    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 10))

    PROVIDER_COOLDOWN: int = int(os.getenv("PROVIDER_COOLDOWN", 60))
    MAX_PROVIDER_SCORE: int = int(os.getenv("MAX_PROVIDER_SCORE", 3))


CFG = Config()


# ============================================================
# LOGGING
# ============================================================

class JsonFormatter(logging.Formatter):

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": self.formatTime(record),
            "level": record.levelname,
            "msg": record.getMessage()
        })


def setup_logger() -> logging.Logger:

    logger = logging.getLogger("GasMonitor")
    logger.setLevel(getattr(logging, CFG.LOG_LEVEL, logging.INFO))
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = JsonFormatter() if CFG.OUTPUT_JSON else logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s"
    )

    file_handler = RotatingFileHandler(
        CFG.LOG_FILE,
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8"
    )

    file_handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)

    return logger


logger = setup_logger()


# ============================================================
# RETRY
# ============================================================

RETRY_ERRORS: tuple[Type[Exception], ...] = (
    Timeout,
    TimeExhausted,
    ProviderConnectionError,
    RequestException,
)


def retry(fn: Callable[..., Any]) -> Callable[..., Any]:

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):

        delay = CFG.RETRY_BASE_DELAY

        for attempt in range(1, CFG.RETRY_LIMIT + 1):

            try:
                return fn(*args, **kwargs)

            except RETRY_ERRORS as e:

                if attempt >= CFG.RETRY_LIMIT:
                    raise

                jitter = random.uniform(0.8, 1.2)
                sleep_time = min(delay * jitter, CFG.RETRY_MAX_DELAY)

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
# PROVIDER HEALTH
# ============================================================

@dataclass
class Provider:

    url: str
    score: int = 0
    cooldown_until: float = 0

    def healthy(self) -> bool:

        if time.time() < self.cooldown_until:
            return False

        return self.score < CFG.MAX_PROVIDER_SCORE

    def penalize(self):

        self.score += 1
        self.cooldown_until = time.time() + CFG.PROVIDER_COOLDOWN

    def recover(self):

        if self.score > 0:
            self.score -= 1


# ============================================================
# WEB3 CLIENT
# ============================================================

class Web3Client:

    def __init__(self, primary: str, fallbacks: List[str]):

        self.providers = [Provider(primary), *[Provider(p) for p in fallbacks]]

        self.web3: Optional[Web3] = None
        self.current: Optional[Provider] = None

        self.session = requests.Session()

    def _select_provider(self) -> Provider:

        healthy = [p for p in self.providers if p.healthy()]

        if not healthy:
            raise ConnectionError("No healthy providers")

        # weighted random (lower score = higher chance)
        weights = [CFG.MAX_PROVIDER_SCORE - p.score for p in healthy]

        return random.choices(healthy, weights=weights, k=1)[0]

    def _connect(self):

        provider = self._select_provider()

        logger.info("Connecting to %s", provider.url)

        w3 = Web3(
            Web3.HTTPProvider(
                provider.url,
                request_kwargs={
                    "timeout": CFG.HTTP_TIMEOUT,
                    "session": self.session
                }
            )
        )

        if not w3.is_connected():

            provider.penalize()
            raise ConnectionError("Provider connection failed")

        self.web3 = w3
        self.current = provider

        provider.recover()

    def get(self) -> Web3:

        if self.web3 and self.web3.is_connected():
            return self.web3

        self._connect()

        return self.web3

    def penalize_current(self):

        if self.current:
            self.current.penalize()

        self.web3 = None
        self.current = None


# ============================================================
# GAS FETCH
# ============================================================

@retry
def fetch_gas(client: Web3Client) -> Dict[str, Any]:

    w3 = client.get()

    block = w3.eth.get_block("pending")

    base_fee = block.get("baseFeePerGas", 0)

    try:

        priority_fee = w3.eth.max_priority_fee

    except Exception:

        gas_price = w3.eth.gas_price
        priority_fee = max(gas_price - base_fee, 0)

    max_fee = base_fee + priority_fee

    return {
        "gas_price_gwei": float(w3.from_wei(max_fee, "gwei")),
        "base_fee_gwei": float(w3.from_wei(base_fee, "gwei")),
        "priority_fee_gwei": float(w3.from_wei(priority_fee, "gwei")),
        "block": block.get("number"),
        "timestamp": int(time.time())
    }


# ============================================================
# SHUTDOWN
# ============================================================

class GracefulShutdown:

    def __init__(self):

        self.event = threading.Event()

        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_):

        logger.info("Shutdown signal received")
        self.event.set()

    def wait(self, timeout: int):

        return self.event.wait(timeout)

    @property
    def stopped(self):

        return self.event.is_set()


# ============================================================
# MAIN LOOP
# ============================================================

def emit(data: Dict[str, Any]):

    if CFG.OUTPUT_JSON:

        print(json.dumps(data))

    else:

        logger.info(
            "Gas %.2f gwei | base %.2f | tip %.2f | block %s",
            data["gas_price_gwei"],
            data["base_fee_gwei"],
            data["priority_fee_gwei"],
            data["block"]
        )


def monitor():

    shutdown = GracefulShutdown()

    client = Web3Client(
        CFG.PROVIDER_URL,
        list(CFG.FALLBACK_PROVIDERS)
    )

    logger.info("Gas monitor started")

    while not shutdown.stopped:

        try:

            data = fetch_gas(client)

            emit(data)

        except Exception as e:

            logger.error("Fetch failed: %s", e)

            client.penalize_current()

        shutdown.wait(CFG.MONITOR_INTERVAL)

    logger.info("Gas monitor stopped")


def main():

    monitor()


if __name__ == "__main__":

    main()
