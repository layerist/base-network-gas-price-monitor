#!/usr/bin/env python3
"""
Ultra-Robust EVM Gas Price Monitor (v3)

Major improvements:
- Persistent Web3 clients (no reconnect every request)
- Real thread-safe circuit breaker
- Fastest-provider race with cancellation
- Better provider health scoring
- Adaptive EIP-1559 fee estimation
- Connection pooling / keepalive tuning
- Decorrelated jitter retry strategy
- Metrics-ready
- Graceful shutdown
- Production-grade failover
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any, Callable, Dict, List, Optional, Type

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry
from web3 import Web3
from web3.exceptions import ProviderConnectionError, TimeExhausted


# ============================================================
# CONFIG
# ============================================================

@dataclass(frozen=True)
class Config:
    PROVIDER_URL: str = os.getenv(
        "PROVIDER_URL",
        "https://mainnet.base.org/v1/infura/YOUR_PROJECT_ID"
    ).strip()

    FALLBACK_PROVIDERS: tuple[str, ...] = (
        "https://base.llamarpc.com",
        "https://base-mainnet.public.blastapi.io",
        "https://base.publicnode.com",
    )

    HTTP_TIMEOUT: int = int(os.getenv("HTTP_TIMEOUT", 8))
    MONITOR_INTERVAL: int = int(os.getenv("MONITOR_INTERVAL", 8))

    RETRY_LIMIT: int = int(os.getenv("RETRY_LIMIT", 5))
    RETRY_BASE_DELAY: float = float(
        os.getenv("RETRY_BASE_DELAY", 0.5)
    )
    RETRY_MAX_DELAY: float = float(
        os.getenv("RETRY_MAX_DELAY", 10)
    )

    PARALLEL_PROBES: int = int(
        os.getenv("PARALLEL_PROBES", 3)
    )

    MAX_PROVIDER_SCORE: int = int(
        os.getenv("MAX_PROVIDER_SCORE", 5)
    )

    HALF_OPEN_AFTER: int = int(
        os.getenv("HALF_OPEN_AFTER", 25)
    )

    COOLDOWN: int = int(
        os.getenv("COOLDOWN", 60)
    )

    LOG_FILE: str = os.getenv(
        "LOG_FILE",
        "gas_monitor.log"
    )

    LOG_LEVEL: str = os.getenv(
        "LOG_LEVEL",
        "INFO"
    ).upper()

    OUTPUT_JSON: bool = (
        os.getenv("OUTPUT_JSON", "false").lower()
        == "true"
    )


CFG = Config()


# ============================================================
# LOGGING
# ============================================================

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "message": record.getMessage()
        })


def setup_logger():
    logger = logging.getLogger("GasMonitor")

    if logger.handlers:
        return logger

    logger.setLevel(
        getattr(logging, CFG.LOG_LEVEL, logging.INFO)
    )
    logger.propagate = False

    formatter = (
        JsonFormatter()
        if CFG.OUTPUT_JSON
        else logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s"
        )
    )

    file_handler = RotatingFileHandler(
        CFG.LOG_FILE,
        maxBytes=5_000_000,
        backupCount=3
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


logger = setup_logger()


# ============================================================
# RETRYABLE ERRORS
# ============================================================

RETRY_ERRORS: tuple[Type[Exception], ...] = (
    Timeout,
    TimeExhausted,
    ProviderConnectionError,
    RequestException,
    ConnectionError,
    ValueError,
)


# ============================================================
# SESSION FACTORY
# ============================================================

def build_session() -> requests.Session:
    session = requests.Session()

    retry = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0
    )

    adapter = HTTPAdapter(
        pool_connections=32,
        pool_maxsize=64,
        max_retries=retry
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        "Connection": "keep-alive",
        "Accept": "application/json",
    })

    return session


# ============================================================
# PROVIDER
# ============================================================

@dataclass
class Provider:
    url: str
    latency: float = 1.0
    score: int = 0

    state: str = "closed"

    last_fail: float = 0.0
    half_open_probe: bool = False

    lock: threading.Lock = field(
        default_factory=threading.Lock
    )

    w3: Optional[Web3] = None

    def available(self) -> bool:
        with self.lock:
            now = time.time()

            if self.state == "closed":
                return True

            if self.state == "open":
                if (
                    now - self.last_fail
                    > CFG.HALF_OPEN_AFTER
                ):
                    self.state = "half-open"

                else:
                    return False

            if self.state == "half-open":
                if self.half_open_probe:
                    return False

                self.half_open_probe = True
                return True

            return True

    def success(self, latency: float):
        with self.lock:
            alpha = 0.2
            self.latency = (
                alpha * latency
                + (1 - alpha) * self.latency
            )

            self.score = max(0, self.score - 1)

            self.state = "closed"
            self.half_open_probe = False

    def fail(self):
        with self.lock:
            self.score += 1
            self.last_fail = time.time()

            self.half_open_probe = False

            if self.score >= CFG.MAX_PROVIDER_SCORE:
                self.state = "open"


# ============================================================
# WEB3 CLIENT
# ============================================================

class Web3Client:

    def __init__(
        self,
        primary: str,
        fallbacks: List[str]
    ):
        urls = [primary] + fallbacks
        random.shuffle(urls)

        self.session = build_session()

        self.providers: List[Provider] = []

        for url in urls:
            provider = Provider(url)

            provider.w3 = Web3(
                Web3.HTTPProvider(
                    url,
                    request_kwargs={
                        "timeout": CFG.HTTP_TIMEOUT,
                        "session": self.session
                    }
                )
            )

            self.providers.append(provider)

        self.executor = ThreadPoolExecutor(
            max_workers=max(
                CFG.PARALLEL_PROBES,
                len(self.providers)
            )
        )

    def _probe(
        self,
        provider: Provider
    ):
        start = time.perf_counter()

        try:
            w3 = provider.w3

            if not w3.is_connected():
                raise ConnectionError(
                    "provider disconnected"
                )

            w3.eth.block_number

            latency = (
                time.perf_counter() - start
            )

            provider.success(latency)

            return (
                w3,
                provider,
                latency
            )

        except Exception:
            provider.fail()
            raise

    def get_fastest(self) -> Web3:

        available = [
            p for p in self.providers
            if p.available()
        ]

        if not available:
            raise ConnectionError(
                "No healthy providers"
            )

        available.sort(
            key=lambda p: (
                p.score,
                p.latency
            )
        )

        selected = available[
            :CFG.PARALLEL_PROBES
        ]

        futures = {
            self.executor.submit(
                self._probe,
                provider
            ): provider
            for provider in selected
        }

        done, pending = wait(
            futures,
            return_when=FIRST_COMPLETED
        )

        for future in done:
            try:
                w3, provider, latency = (
                    future.result()
                )

                for p in pending:
                    p.cancel()

                logger.debug(
                    "Provider selected %s "
                    "(lat=%.3fs score=%d)",
                    provider.url,
                    latency,
                    provider.score
                )

                return w3

            except Exception:
                continue

        raise ConnectionError(
            "All providers failed"
        )

    def close(self):
        self.executor.shutdown(
            wait=False,
            cancel_futures=True
        )
        self.session.close()


# ============================================================
# RETRY
# ============================================================

def retry(fn):

    @wraps(fn)
    def wrapper(*args, **kwargs):

        sleep = CFG.RETRY_BASE_DELAY

        for attempt in range(
            1,
            CFG.RETRY_LIMIT + 1
        ):
            try:
                return fn(*args, **kwargs)

            except RETRY_ERRORS as e:

                if attempt >= CFG.RETRY_LIMIT:
                    raise

                sleep = min(
                    random.uniform(
                        CFG.RETRY_BASE_DELAY,
                        sleep * 3
                    ),
                    CFG.RETRY_MAX_DELAY
                )

                logger.warning(
                    "Retry %d/%d "
                    "in %.2fs (%s)",
                    attempt,
                    CFG.RETRY_LIMIT,
                    sleep,
                    e,
                )

                time.sleep(sleep)

    return wrapper


# ============================================================
# GAS FETCH
# ============================================================

@retry
def fetch_gas(
    client: Web3Client
) -> Dict[str, Any]:

    w3 = client.get_fastest()

    block = w3.eth.get_block("pending")

    base_fee = block.get(
        "baseFeePerGas"
    )

    if base_fee is None:
        gas_price = w3.eth.gas_price

        return {
            "gas_price_gwei":
                float(
                    w3.from_wei(
                        gas_price,
                        "gwei"
                    )
                ),
            "base_fee_gwei": None,
            "priority_fee_gwei": None,
            "block":
                block.get("number"),
            "timestamp":
                int(time.time())
        }

    try:
        history = w3.eth.fee_history(
            10,
            "latest",
            [20, 50, 80]
        )

        rewards = []

        for row in history["reward"]:
            rewards.extend(
                [
                    int(v)
                    for v in row
                    if v > 0
                ]
            )

        if rewards:
            priority_fee = int(
                statistics.median(
                    rewards
                )
            )
        else:
            priority_fee = (
                w3.eth.max_priority_fee
            )

    except Exception:
        priority_fee = int(1e9)

    max_fee = int(
        base_fee * 1.25
        + priority_fee
    )

    return {
        "gas_price_gwei":
            float(
                w3.from_wei(
                    max_fee,
                    "gwei"
                )
            ),
        "base_fee_gwei":
            float(
                w3.from_wei(
                    base_fee,
                    "gwei"
                )
            ),
        "priority_fee_gwei":
            float(
                w3.from_wei(
                    priority_fee,
                    "gwei"
                )
            ),
        "block":
            block.get("number"),
        "timestamp":
            int(time.time())
    }


# ============================================================
# SHUTDOWN
# ============================================================

class GracefulShutdown:

    def __init__(self):
        self.event = threading.Event()

        signal.signal(
            signal.SIGINT,
            self._handler
        )

        signal.signal(
            signal.SIGTERM,
            self._handler
        )

    def _handler(self, *_):
        logger.info(
            "Shutdown signal received"
        )
        self.event.set()

    @property
    def stopped(self):
        return self.event.is_set()

    def wait(self, timeout):
        return self.event.wait(timeout)


# ============================================================
# OUTPUT
# ============================================================

def emit(data):

    if CFG.OUTPUT_JSON:
        print(json.dumps(data))
        return

    logger.info(
        "Gas %.2f gwei | "
        "base %.2f | "
        "tip %.2f | "
        "block %s",
        data["gas_price_gwei"],
        data["base_fee_gwei"]
        or 0,
        data["priority_fee_gwei"]
        or 0,
        data["block"]
    )


# ============================================================
# MAIN LOOP
# ============================================================

def monitor():

    shutdown = GracefulShutdown()

    client = Web3Client(
        CFG.PROVIDER_URL,
        list(
            CFG.FALLBACK_PROVIDERS
        )
    )

    logger.info(
        "Gas monitor started"
    )

    try:
        while not shutdown.stopped:

            try:
                data = fetch_gas(client)
                emit(data)

            except Exception as e:
                logger.exception(
                    "Fetch failed: %s",
                    e
                )

            shutdown.wait(
                CFG.MONITOR_INTERVAL
            )

    finally:
        client.close()

        logger.info(
            "Gas monitor stopped"
        )


def main():
    monitor()


if __name__ == "__main__":
    main()
