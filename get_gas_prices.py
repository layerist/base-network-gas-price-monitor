#!/usr/bin/env python3
"""
Ultra-Robust EVM Gas Price Monitor (v4)

What this version improves:
- Env-based provider list: PROVIDER_URLS / PROVIDER_URL / FALLBACK_PROVIDERS
- Filters placeholder/empty provider URLs before startup
- Per-provider Web3 + requests.Session keep-alive pools
- Thread-safe circuit breaker with real cooldown and half-open probes
- Race logic waits for the first successful provider, not merely the first finished one
- Better fee estimation with safe fallback order
- Optional RPC proxy support via RPC_PROXY / HTTPS_PROXY / HTTP_PROXY
- Startup config validation
- Cleaner JSON/text output
- Graceful shutdown and provider cleanup
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import statistics
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from functools import wraps
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypeVar, Callable, cast

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry
from web3 import Web3
from web3.exceptions import ProviderConnectionError, TimeExhausted


# ============================================================
# CONFIG HELPERS
# ============================================================


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, min_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _env_float(name: str, default: float, min_value: Optional[float] = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a float, got {raw!r}") from exc
    if min_value is not None and value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    return value


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _unique(items: Iterable[str]) -> Tuple[str, ...]:
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _is_usable_url(url: str) -> bool:
    if not url:
        return False
    lowered = url.lower()
    if "your_project_id" in lowered or "your-api-key" in lowered or "api_key" in lowered:
        return False
    return lowered.startswith(("http://", "https://"))


@dataclass(frozen=True)
class Config:
    # Prefer PROVIDER_URLS="url1,url2,url3". PROVIDER_URL is kept for backward compatibility.
    provider_urls: Tuple[str, ...] = field(default_factory=lambda: _unique(
        [
            *_split_csv(os.getenv("PROVIDER_URLS", "")),
            *([os.getenv("PROVIDER_URL", "").strip()] if os.getenv("PROVIDER_URL") else []),
            *_split_csv(os.getenv("FALLBACK_PROVIDERS", "")),
            "https://base.llamarpc.com",
            "https://base-mainnet.public.blastapi.io",
            "https://base.publicnode.com",
        ]
    ))

    http_timeout: float = field(default_factory=lambda: _env_float("HTTP_TIMEOUT", 8.0, 0.2))
    monitor_interval: float = field(default_factory=lambda: _env_float("MONITOR_INTERVAL", 8.0, 0.1))

    retry_limit: int = field(default_factory=lambda: _env_int("RETRY_LIMIT", 5, 1))
    retry_base_delay: float = field(default_factory=lambda: _env_float("RETRY_BASE_DELAY", 0.5, 0.0))
    retry_max_delay: float = field(default_factory=lambda: _env_float("RETRY_MAX_DELAY", 10.0, 0.1))

    parallel_probes: int = field(default_factory=lambda: _env_int("PARALLEL_PROBES", 3, 1))
    max_provider_score: int = field(default_factory=lambda: _env_int("MAX_PROVIDER_SCORE", 5, 1))
    cooldown_sec: float = field(default_factory=lambda: _env_float("COOLDOWN", 60.0, 1.0))
    half_open_after_sec: float = field(default_factory=lambda: _env_float("HALF_OPEN_AFTER", 25.0, 1.0))

    fee_history_blocks: int = field(default_factory=lambda: _env_int("FEE_HISTORY_BLOCKS", 10, 1))
    fee_reward_percentiles: Tuple[float, ...] = (20.0, 50.0, 80.0)
    max_fee_multiplier: float = field(default_factory=lambda: _env_float("MAX_FEE_MULTIPLIER", 1.25, 1.0))
    fallback_priority_fee_gwei: float = field(default_factory=lambda: _env_float("FALLBACK_PRIORITY_FEE_GWEI", 1.0, 0.0))

    rpc_proxy: str = os.getenv("RPC_PROXY", "").strip()
    log_file: str = os.getenv("LOG_FILE", "gas_monitor.log").strip()
    log_level: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    output_json: bool = field(default_factory=lambda: _env_bool("OUTPUT_JSON", False))

    def validated_provider_urls(self) -> Tuple[str, ...]:
        urls = tuple(url for url in self.provider_urls if _is_usable_url(url))
        if not urls:
            raise ValueError(
                "No usable RPC providers. Set PROVIDER_URLS='https://rpc1,https://rpc2' "
                "or PROVIDER_URL='https://rpc'."
            )
        return urls


CFG = Config()


# ============================================================
# LOGGING
# ============================================================


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logger() -> logging.Logger:
    logger_ = logging.getLogger("GasMonitor")
    if logger_.handlers:
        return logger_

    logger_.setLevel(getattr(logging, CFG.log_level, logging.INFO))
    logger_.propagate = False

    formatter: logging.Formatter = JsonFormatter() if CFG.output_json else logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s"
    )

    if CFG.log_file:
        file_handler = RotatingFileHandler(CFG.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger_.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger_.addHandler(console_handler)

    return logger_


logger = setup_logger()


# ============================================================
# ERRORS / RETRY
# ============================================================


RETRY_ERRORS: Tuple[type[Exception], ...] = (
    Timeout,
    TimeExhausted,
    ProviderConnectionError,
    RequestException,
    ConnectionError,
    ValueError,
)

F = TypeVar("F", bound=Callable[..., Any])


def retry(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        sleep = CFG.retry_base_delay

        for attempt in range(1, CFG.retry_limit + 1):
            try:
                return fn(*args, **kwargs)
            except RETRY_ERRORS as exc:
                if attempt >= CFG.retry_limit:
                    raise

                upper = max(CFG.retry_base_delay, sleep * 3)
                sleep = min(random.uniform(CFG.retry_base_delay, upper), CFG.retry_max_delay)

                logger.warning(
                    "Retry %d/%d in %.2fs: %s",
                    attempt,
                    CFG.retry_limit,
                    sleep,
                    exc,
                )
                time.sleep(sleep)

        raise RuntimeError("unreachable retry state")

    return cast(F, wrapper)


# ============================================================
# HTTP / WEB3 PROVIDERS
# ============================================================


def build_session(proxy: str = "") -> requests.Session:
    session = requests.Session()

    # Web3 calls are idempotent reads here; we keep urllib retries disabled and use our own retry layer.
    retry_cfg = Retry(total=0, connect=0, read=0, redirect=0)
    adapter = HTTPAdapter(pool_connections=32, pool_maxsize=64, max_retries=retry_cfg)

    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"Connection": "keep-alive", "Accept": "application/json"})

    if proxy:
        session.trust_env = False
        session.proxies.update({"http": proxy, "https": proxy})

    return session


@dataclass
class Provider:
    url: str
    session: requests.Session
    w3: Web3
    latency: float = 1.0
    score: int = 0
    state: str = "closed"  # closed | open | half-open
    last_fail: float = 0.0
    half_open_probe: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def create(cls, url: str) -> "Provider":
        session = build_session(CFG.rpc_proxy)
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": CFG.http_timeout, "session": session}))
        return cls(url=url, session=session, w3=w3)

    def available(self) -> bool:
        with self.lock:
            now = time.time()

            if self.state == "closed":
                return True

            if self.state == "open":
                cooldown = max(CFG.cooldown_sec, CFG.half_open_after_sec)
                if now - self.last_fail < cooldown:
                    return False
                self.state = "half-open"

            if self.state == "half-open":
                if self.half_open_probe:
                    return False
                self.half_open_probe = True
                return True

            return False

    def success(self, latency: float) -> None:
        with self.lock:
            alpha = 0.25
            self.latency = alpha * latency + (1 - alpha) * self.latency
            self.score = max(0, self.score - 1)
            self.state = "closed"
            self.half_open_probe = False

    def fail(self) -> None:
        with self.lock:
            self.score += 1
            self.last_fail = time.time()
            self.half_open_probe = False
            if self.score >= CFG.max_provider_score:
                self.state = "open"

    def close(self) -> None:
        self.session.close()


class Web3Client:
    def __init__(self, urls: Iterable[str]) -> None:
        unique_urls = list(_unique(urls))
        random.shuffle(unique_urls)

        self.providers = [Provider.create(url) for url in unique_urls]
        self.executor = ThreadPoolExecutor(max_workers=max(CFG.parallel_probes, len(self.providers)))

        logger.info("Loaded %d RPC provider(s)", len(self.providers))

    def _probe(self, provider: Provider) -> Tuple[Web3, Provider, float, int]:
        start = time.perf_counter()
        try:
            # block_number is enough as a lightweight health check and avoids an extra is_connected RPC call.
            block_number = provider.w3.eth.block_number
            latency = time.perf_counter() - start
            provider.success(latency)
            return provider.w3, provider, latency, int(block_number)
        except Exception:
            provider.fail()
            raise

    def get_fastest(self) -> Web3:
        available = [provider for provider in self.providers if provider.available()]
        if not available:
            raise ConnectionError("No healthy RPC providers")

        available.sort(key=lambda provider: (provider.score, provider.latency))
        selected = available[: min(CFG.parallel_probes, len(available))]

        futures: Dict[Future[Tuple[Web3, Provider, float, int]], Provider] = {
            self.executor.submit(self._probe, provider): provider for provider in selected
        }

        last_error: Optional[BaseException] = None
        pending = set(futures)

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                provider = futures[future]
                try:
                    w3, selected_provider, latency, block_number = future.result()
                    for pending_future in pending:
                        pending_future.cancel()
                    logger.debug(
                        "Selected RPC %s | latency=%.3fs | score=%d | block=%s",
                        selected_provider.url,
                        latency,
                        selected_provider.score,
                        block_number,
                    )
                    return w3
                except BaseException as exc:
                    last_error = exc
                    logger.debug("RPC probe failed for %s: %s", provider.url, exc)

        raise ConnectionError(f"All selected RPC providers failed: {last_error}")

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
        for provider in self.providers:
            provider.close()


# ============================================================
# GAS FETCH
# ============================================================


def _wei_to_gwei(w3: Web3, value_wei: int) -> float:
    return float(w3.from_wei(int(value_wei), "gwei"))


def _estimate_priority_fee(w3: Web3) -> int:
    try:
        history = w3.eth.fee_history(
            CFG.fee_history_blocks,
            "latest",
            list(CFG.fee_reward_percentiles),
        )
        rewards: List[int] = []
        for row in history.get("reward", []):
            rewards.extend(int(value) for value in row if int(value) > 0)
        if rewards:
            return int(statistics.median(rewards))
    except Exception as exc:
        logger.debug("fee_history failed: %s", exc)

    try:
        return int(w3.eth.max_priority_fee)
    except Exception as exc:
        logger.debug("max_priority_fee failed: %s", exc)

    return int(CFG.fallback_priority_fee_gwei * 1_000_000_000)


@retry
def fetch_gas(client: Web3Client) -> Dict[str, Any]:
    w3 = client.get_fastest()
    block = w3.eth.get_block("pending")
    base_fee = block.get("baseFeePerGas")
    block_number = block.get("number")

    if base_fee is None:
        gas_price = int(w3.eth.gas_price)
        return {
            "type": "legacy",
            "gas_price_gwei": _wei_to_gwei(w3, gas_price),
            "base_fee_gwei": None,
            "priority_fee_gwei": None,
            "max_fee_gwei": None,
            "block": block_number,
            "timestamp": int(time.time()),
        }

    base_fee_int = int(base_fee)
    priority_fee = _estimate_priority_fee(w3)
    max_fee = int(base_fee_int * CFG.max_fee_multiplier + priority_fee)

    return {
        "type": "eip1559",
        "gas_price_gwei": _wei_to_gwei(w3, max_fee),
        "base_fee_gwei": _wei_to_gwei(w3, base_fee_int),
        "priority_fee_gwei": _wei_to_gwei(w3, priority_fee),
        "max_fee_gwei": _wei_to_gwei(w3, max_fee),
        "block": block_number,
        "timestamp": int(time.time()),
    }


# ============================================================
# SHUTDOWN / OUTPUT / MAIN
# ============================================================


class GracefulShutdown:
    def __init__(self) -> None:
        self.event = threading.Event()
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is not None:
                try:
                    signal.signal(sig, self._handler)
                except (ValueError, OSError):
                    # Signal registration may fail outside the main thread or on some platforms.
                    pass

    def _handler(self, *_: Any) -> None:
        logger.info("Shutdown signal received")
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()

    def wait(self, timeout: float) -> bool:
        return self.event.wait(timeout)


def emit(data: Dict[str, Any]) -> None:
    if CFG.output_json:
        print(json.dumps(data, ensure_ascii=False), flush=True)
        return

    logger.info(
        "Gas %.4f gwei | base %.4f | tip %.4f | block %s | %s",
        data["gas_price_gwei"],
        data["base_fee_gwei"] or 0.0,
        data["priority_fee_gwei"] or 0.0,
        data["block"],
        data["type"],
    )


def monitor() -> None:
    urls = CFG.validated_provider_urls()
    shutdown = GracefulShutdown()
    client = Web3Client(urls)

    logger.info("Gas monitor started | interval=%.2fs | probes=%d", CFG.monitor_interval, CFG.parallel_probes)

    try:
        while not shutdown.stopped:
            try:
                emit(fetch_gas(client))
            except Exception as exc:
                logger.exception("Fetch failed: %s", exc)

            shutdown.wait(CFG.monitor_interval)
    finally:
        client.close()
        logger.info("Gas monitor stopped")


def main() -> int:
    try:
        monitor()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
