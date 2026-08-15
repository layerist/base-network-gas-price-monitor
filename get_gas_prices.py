#!/usr/bin/env python3
"""
Ultra-Robust EVM Gas Price Monitor (v6)

Highlights:
- Races the complete gas fetch, not a separate health check.
- Per-provider circuit breaker with CLOSED / OPEN / HALF_OPEN states.
- Provider failures are attributed to the provider that actually failed.
- Uses monotonic clocks for latency, cooldowns, and scheduling.
- Safe provider defaults: built-in Base RPCs are used only when no custom RPC is configured.
- Built-in Base defaults automatically enforce chain_id=8453 unless explicitly overridden.
- Wrong-chain providers are permanently disabled for the current process.
- EIP-1559 priority fee uses one configured reward percentile across blocks.
- Bounds suspicious priority fees and supports minimum/maximum fee caps.
- Reuses HTTP connections through one requests.Session per provider.
- Redacts credentials/API keys from logs.
- JSON output stays on stdout; logs stay on stderr/file.
- Full quote latency includes priority-fee estimation.
- Retry backoff is interruptible for faster graceful shutdown.
- Atomic interval scheduling avoids cumulative loop drift.
- Graceful shutdown and deterministic cleanup.

Dependencies:
    pip install "web3>=6,<8" "requests>=2.31,<3"

Typical environment:
    PROVIDER_URLS=https://base.publicnode.com,https://base.llamarpc.com
    EXPECTED_CHAIN_ID=8453
    OUTPUT_JSON=true
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import signal
import statistics
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import SplitResult, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry
from web3 import Web3
from web3.exceptions import ProviderConnectionError, TimeExhausted


APP_NAME = "GasMonitor"
GWEI = Decimal(1_000_000_000)


# ============================================================
# CONFIG
# ============================================================


class ConfigError(ValueError):
    """Raised when environment configuration is invalid."""


def env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def env_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc

    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def env_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc

    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite, got {value}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


def env_decimal(
    name: str,
    default: str,
    *,
    minimum: Optional[Decimal] = None,
) -> Decimal:
    raw = env_text(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {raw!r}") from exc

    if not value.is_finite():
        raise ConfigError(f"{name} must be finite, got {value}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def usable_http_url(value: str) -> bool:
    if not value:
        return False

    lowered = value.lower()
    placeholders = (
        "your_project_id",
        "your-project-id",
        "your_api_key",
        "your-api-key",
        "<api",
        "${",
    )
    if any(marker in lowered for marker in placeholders):
        return False

    try:
        parsed = urlsplit(value)
    except ValueError:
        return False

    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


@dataclass(frozen=True, slots=True)
class Config:
    provider_urls: tuple[str, ...]
    expected_chain_id: Optional[int]

    http_timeout: float
    monitor_interval: float
    parallel_requests: int

    retry_rounds: int
    retry_base_delay: float
    retry_max_delay: float

    failure_threshold: int
    cooldown_seconds: float
    latency_ewma_alpha: float

    fee_history_blocks: int
    priority_percentile: float
    fallback_priority_fee_gwei: Decimal
    min_priority_fee_gwei: Decimal
    max_priority_fee_gwei: Decimal
    max_fee_multiplier: Decimal
    max_fee_cap_gwei: Decimal

    block_tag: str
    rpc_proxy: str
    trust_env_proxy: bool

    output_json: bool
    log_json: bool
    log_level: str
    log_file: str
    log_max_bytes: int
    log_backups: int

    @classmethod
    def from_env(cls) -> "Config":
        raw_custom_urls = unique(
            [
                *split_csv(env_text("PROVIDER_URLS")),
                *([env_text("PROVIDER_URL")] if env_text("PROVIDER_URL") else []),
                *split_csv(env_text("FALLBACK_PROVIDERS")),
            ]
        )
        invalid_custom_urls = tuple(
            url for url in raw_custom_urls if not usable_http_url(url)
        )
        if invalid_custom_urls:
            raise ConfigError(
                f"Found {len(invalid_custom_urls)} invalid/placeholder custom RPC URL(s). "
                "Fix or remove them instead of silently falling back to another endpoint."
            )
        custom_urls = raw_custom_urls

        builtin_base_urls = (
            "https://base.llamarpc.com",
            "https://base-mainnet.public.blastapi.io",
            "https://base.publicnode.com",
        )
        include_builtin_base = env_bool("INCLUDE_BUILTIN_BASE_PROVIDERS", False)
        using_builtin_defaults = not custom_urls

        raw_urls = (
            (*custom_urls, *builtin_base_urls)
            if custom_urls and include_builtin_base
            else custom_urls
            if custom_urls
            else builtin_base_urls
        )
        urls = unique(raw_urls)
        if not urls:
            raise ConfigError(
                "No usable RPC endpoints. Set PROVIDER_URLS='https://rpc1,https://rpc2'."
            )

        expected_chain_raw = env_text("EXPECTED_CHAIN_ID")
        if expected_chain_raw:
            expected_chain_id = env_int("EXPECTED_CHAIN_ID", 1, minimum=1)
        elif using_builtin_defaults or include_builtin_base:
            # Built-in endpoints are Base mainnet. Enforce this automatically so
            # a custom/misconfigured endpoint can never silently mix chains.
            expected_chain_id = 8453
        else:
            expected_chain_id = None

        cfg = cls(
            provider_urls=urls,
            expected_chain_id=expected_chain_id,
            http_timeout=env_float("HTTP_TIMEOUT", 8.0, minimum=0.2),
            monitor_interval=env_float("MONITOR_INTERVAL", 8.0, minimum=0.1),
            parallel_requests=env_int("PARALLEL_REQUESTS", 3, minimum=1),
            retry_rounds=env_int("RETRY_ROUNDS", 3, minimum=1),
            retry_base_delay=env_float("RETRY_BASE_DELAY", 0.4, minimum=0.0),
            retry_max_delay=env_float("RETRY_MAX_DELAY", 8.0, minimum=0.1),
            failure_threshold=env_int("FAILURE_THRESHOLD", 3, minimum=1),
            cooldown_seconds=env_float("COOLDOWN_SECONDS", 30.0, minimum=0.1),
            latency_ewma_alpha=env_float(
                "LATENCY_EWMA_ALPHA", 0.25, minimum=0.01, maximum=1.0
            ),
            fee_history_blocks=env_int("FEE_HISTORY_BLOCKS", 10, minimum=1, maximum=1024),
            priority_percentile=env_float(
                "PRIORITY_PERCENTILE", 50.0, minimum=0.0, maximum=100.0
            ),
            fallback_priority_fee_gwei=env_decimal(
                "FALLBACK_PRIORITY_FEE_GWEI", "0.001", minimum=Decimal(0)
            ),
            min_priority_fee_gwei=env_decimal(
                "MIN_PRIORITY_FEE_GWEI", "0", minimum=Decimal(0)
            ),
            max_priority_fee_gwei=env_decimal(
                "MAX_PRIORITY_FEE_GWEI", "5", minimum=Decimal(0)
            ),
            max_fee_multiplier=env_decimal(
                "MAX_FEE_MULTIPLIER", "1.25", minimum=Decimal(1)
            ),
            max_fee_cap_gwei=env_decimal(
                "MAX_FEE_CAP_GWEI", "0", minimum=Decimal(0)
            ),
            block_tag=env_text("BLOCK_TAG", "pending").lower(),
            rpc_proxy=env_text("RPC_PROXY"),
            trust_env_proxy=env_bool("TRUST_ENV_PROXY", True),
            output_json=env_bool("OUTPUT_JSON", False),
            log_json=env_bool("LOG_JSON", False),
            log_level=env_text("LOG_LEVEL", "INFO").upper(),
            log_file=env_text("LOG_FILE", "gas_monitor.log"),
            log_max_bytes=env_int("LOG_MAX_BYTES", 5_000_000, minimum=0),
            log_backups=env_int("LOG_BACKUPS", 3, minimum=0),
        )

        valid_log_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if cfg.log_level not in valid_log_levels:
            raise ConfigError(
                f"LOG_LEVEL must be one of {sorted(valid_log_levels)}, got {cfg.log_level!r}"
            )
        if cfg.block_tag not in {"latest", "pending", "safe", "finalized"}:
            raise ConfigError(
                "BLOCK_TAG must be latest, pending, safe, or finalized"
            )
        if cfg.retry_max_delay < cfg.retry_base_delay:
            raise ConfigError("RETRY_MAX_DELAY must be >= RETRY_BASE_DELAY")
        if cfg.max_priority_fee_gwei and (
            cfg.min_priority_fee_gwei > cfg.max_priority_fee_gwei
        ):
            raise ConfigError(
                "MIN_PRIORITY_FEE_GWEI must be <= MAX_PRIORITY_FEE_GWEI"
            )

        return cfg


# ============================================================
# LOGGING
# ============================================================


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logger(cfg: Config) -> logging.Logger:
    logger_ = logging.getLogger(APP_NAME)
    logger_.handlers.clear()
    logger_.setLevel(getattr(logging, cfg.log_level, logging.INFO))
    logger_.propagate = False

    formatter: logging.Formatter
    if cfg.log_json:
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(message)s"
        )

    # StreamHandler writes to stderr, preserving stdout for machine-readable output.
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger_.addHandler(console)

    if cfg.log_file:
        file_handler = RotatingFileHandler(
            cfg.log_file,
            maxBytes=cfg.log_max_bytes,
            backupCount=cfg.log_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger_.addHandler(file_handler)

    return logger_


# ============================================================
# PROVIDERS / CIRCUIT BREAKER
# ============================================================


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"
    DISABLED = "disabled"


class RpcError(RuntimeError):
    """A provider-specific RPC operation failed."""


class WrongChainError(RpcError):
    """RPC endpoint belongs to another chain."""


class NoUsableProviderError(RpcError):
    """No provider can become usable without a configuration change."""


def redact_url(url: str) -> str:
    """Hide credentials, query values, and long path API keys in logs."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "unknown"
        if parsed.port:
            host = f"{host}:{parsed.port}"

        path_parts = [part for part in parsed.path.split("/") if part]
        safe_parts: list[str] = []
        for part in path_parts:
            if len(part) > 12:
                safe_parts.append("***")
            else:
                safe_parts.append(part)

        safe_path = "/" + "/".join(safe_parts) if safe_parts else ""
        safe = SplitResult(parsed.scheme, host, safe_path, "", "")
        return urlunsplit(safe)
    except Exception:
        return "<redacted-rpc>"


def build_session(cfg: Config) -> requests.Session:
    session = requests.Session()

    # Retries are handled at the provider-race level, not hidden inside urllib3.
    no_retry = Retry(total=0, connect=0, read=0, redirect=0, status=0)
    adapter = HTTPAdapter(
        pool_connections=8,
        pool_maxsize=max(4, cfg.parallel_requests * 2),
        max_retries=no_retry,
        pool_block=False,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": f"{APP_NAME}/6",
        }
    )

    session.trust_env = cfg.trust_env_proxy
    if cfg.rpc_proxy:
        session.trust_env = False
        session.proxies.update({"http": cfg.rpc_proxy, "https": cfg.rpc_proxy})

    return session


@dataclass(slots=True)
class Provider:
    url: str
    safe_url: str
    session: requests.Session
    w3: Web3

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    latency_ewma: float = 1.0
    opened_at: float = 0.0
    in_flight: bool = False
    last_error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(cls, url: str, cfg: Config) -> "Provider":
        session = build_session(cfg)
        provider = Web3.HTTPProvider(
            url,
            request_kwargs={
                "timeout": (cfg.http_timeout, cfg.http_timeout),
            },
            session=session,
        )
        return cls(
            url=url,
            safe_url=redact_url(url),
            session=session,
            w3=Web3(provider),
        )

    def try_acquire(self, now: float, cfg: Config) -> bool:
        with self.lock:
            if self.in_flight or self.state is CircuitState.DISABLED:
                return False

            if self.state is CircuitState.OPEN:
                if now - self.opened_at < cfg.cooldown_seconds:
                    return False
                self.state = CircuitState.HALF_OPEN

            if self.state in {CircuitState.CLOSED, CircuitState.HALF_OPEN}:
                self.in_flight = True
                return True

            return False

    def record_success(self, latency: float, cfg: Config) -> None:
        with self.lock:
            alpha = cfg.latency_ewma_alpha
            self.latency_ewma = (
                alpha * latency + (1.0 - alpha) * self.latency_ewma
            )
            self.consecutive_failures = 0
            self.total_successes += 1
            self.state = CircuitState.CLOSED
            self.in_flight = False
            self.last_error = ""

    def record_failure(self, error: BaseException, cfg: Config) -> None:
        with self.lock:
            self.consecutive_failures += 1
            self.total_failures += 1
            self.last_error = f"{type(error).__name__}: {error}"
            self.in_flight = False

            if isinstance(error, WrongChainError):
                # A wrong-chain endpoint is configuration-invalid, not transient.
                # Do not waste retries or allow it back through HALF_OPEN.
                self.state = CircuitState.DISABLED
                return

            if (
                self.state is CircuitState.HALF_OPEN
                or self.consecutive_failures >= cfg.failure_threshold
            ):
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()

    def ranking(self) -> tuple[int, int, float, int]:
        with self.lock:
            state_rank = {
                CircuitState.CLOSED: 0,
                CircuitState.HALF_OPEN: 1,
                CircuitState.OPEN: 2,
                CircuitState.DISABLED: 3,
            }[self.state]
            return (
                state_rank,
                self.consecutive_failures,
                self.latency_ewma,
                self.total_failures,
            )

    def release_unstarted(self) -> None:
        """Release a provider when its queued future was cancelled before execution."""
        with self.lock:
            self.in_flight = False

    def close(self) -> None:
        self.session.close()


# ============================================================
# FEE CALCULATION
# ============================================================


def decimal_to_wei(value_gwei: Decimal) -> int:
    return int((value_gwei * GWEI).to_integral_value(rounding=ROUND_CEILING))


def wei_to_gwei_text(value_wei: int) -> str:
    value = Decimal(int(value_wei)) / GWEI
    # Fixed-point output avoids scientific notation and float precision loss.
    return format(value.normalize(), "f")


def clamp(value: int, minimum: int, maximum: int) -> int:
    if maximum > 0:
        return min(maximum, max(minimum, value))
    return max(minimum, value)


def median_int(values: Sequence[int]) -> int:
    if not values:
        raise ValueError("median_int requires at least one value")
    return int(statistics.median(values))


@dataclass(frozen=True, slots=True)
class GasQuote:
    tx_type: str
    chain_id: int
    provider: str
    block_tag: str
    block_number: Optional[int]
    gas_price_wei: int
    base_fee_wei: Optional[int]
    priority_fee_wei: Optional[int]
    max_fee_wei: Optional[int]
    priority_source: Optional[str]
    latency_ms: int
    timestamp: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.tx_type,
            "chain_id": self.chain_id,
            "provider": self.provider,
            "block_tag": self.block_tag,
            "block": self.block_number,
            "gas_price_wei": self.gas_price_wei,
            "gas_price_gwei": wei_to_gwei_text(self.gas_price_wei),
            "base_fee_wei": self.base_fee_wei,
            "base_fee_gwei": (
                wei_to_gwei_text(self.base_fee_wei)
                if self.base_fee_wei is not None
                else None
            ),
            "priority_fee_wei": self.priority_fee_wei,
            "priority_fee_gwei": (
                wei_to_gwei_text(self.priority_fee_wei)
                if self.priority_fee_wei is not None
                else None
            ),
            "max_fee_wei": self.max_fee_wei,
            "max_fee_gwei": (
                wei_to_gwei_text(self.max_fee_wei)
                if self.max_fee_wei is not None
                else None
            ),
            "priority_source": self.priority_source,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp,
        }


def estimate_priority_fee(w3: Web3, cfg: Config) -> tuple[int, str]:
    minimum = decimal_to_wei(cfg.min_priority_fee_gwei)
    maximum = decimal_to_wei(cfg.max_priority_fee_gwei)

    try:
        history: Mapping[str, Any] = w3.eth.fee_history(
            cfg.fee_history_blocks,
            "latest",
            [cfg.priority_percentile],
        )
        rewards: list[int] = []
        for row in history.get("reward", []):
            if row:
                value = int(row[0])
                if value >= 0:
                    rewards.append(value)

        if rewards:
            return clamp(median_int(rewards), minimum, maximum), "fee_history"
    except Exception:
        pass

    try:
        value = int(w3.eth.max_priority_fee)
        return clamp(value, minimum, maximum), "eth_maxPriorityFeePerGas"
    except Exception:
        fallback = decimal_to_wei(cfg.fallback_priority_fee_gwei)
        return clamp(fallback, minimum, maximum), "configured_fallback"


def fetch_quote(provider: Provider, cfg: Config) -> GasQuote:
    started = time.perf_counter()
    w3 = provider.w3

    chain_id = int(w3.eth.chain_id)
    if cfg.expected_chain_id is not None and chain_id != cfg.expected_chain_id:
        raise WrongChainError(
            f"expected chain {cfg.expected_chain_id}, got {chain_id}"
        )

    block: Mapping[str, Any] = w3.eth.get_block(cfg.block_tag)
    base_fee_raw = block.get("baseFeePerGas")
    block_number_raw = block.get("number")
    block_number = (
        int(block_number_raw) if block_number_raw is not None else None
    )

    if base_fee_raw is None:
        gas_price = int(w3.eth.gas_price)
        latency_ms = round((time.perf_counter() - started) * 1000)
        return GasQuote(
            tx_type="legacy",
            chain_id=chain_id,
            provider=provider.safe_url,
            block_tag=cfg.block_tag,
            block_number=block_number,
            gas_price_wei=gas_price,
            base_fee_wei=None,
            priority_fee_wei=None,
            max_fee_wei=None,
            priority_source=None,
            latency_ms=latency_ms,
            timestamp=int(time.time()),
        )

    base_fee = int(base_fee_raw)
    priority_fee, priority_source = estimate_priority_fee(w3, cfg)
    latency_ms = round((time.perf_counter() - started) * 1000)

    multiplier_scaled = int(
        (Decimal(base_fee) * cfg.max_fee_multiplier).to_integral_value(
            rounding=ROUND_CEILING
        )
    )
    max_fee = multiplier_scaled + priority_fee

    cap = decimal_to_wei(cfg.max_fee_cap_gwei)
    if cap > 0:
        # Never cap below the amount required to include the current base fee + tip.
        max_fee = max(base_fee + priority_fee, min(max_fee, cap))

    return GasQuote(
        tx_type="eip1559",
        chain_id=chain_id,
        provider=provider.safe_url,
        block_tag=cfg.block_tag,
        block_number=block_number,
        gas_price_wei=max_fee,
        base_fee_wei=base_fee,
        priority_fee_wei=priority_fee,
        max_fee_wei=max_fee,
        priority_source=priority_source,
        latency_ms=latency_ms,
        timestamp=int(time.time()),
    )


TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    Timeout,
    TimeExhausted,
    ProviderConnectionError,
    RequestException,
    ConnectionError,
    OSError,
    ValueError,
)


class RpcPool:
    def __init__(self, cfg: Config, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.logger = logger

        urls = list(cfg.provider_urls)
        random.shuffle(urls)
        self.providers = [Provider.create(url, cfg) for url in urls]
        self.executor = ThreadPoolExecutor(
            max_workers=min(
                len(self.providers),
                max(cfg.parallel_requests, 1),
            ),
            thread_name_prefix="rpc",
        )

        logger.info(
            "Loaded %d RPC provider(s); parallel=%d; expected_chain_id=%s",
            len(self.providers),
            min(cfg.parallel_requests, len(self.providers)),
            cfg.expected_chain_id if cfg.expected_chain_id is not None else "any",
        )
        if cfg.expected_chain_id is None and len(self.providers) > 1:
            logger.warning(
                "EXPECTED_CHAIN_ID is not set while multiple custom RPCs are configured; "
                "set it to prevent accidental cross-chain mixing."
            )

    def _run_provider(self, provider: Provider) -> GasQuote:
        started = time.perf_counter()
        try:
            quote = fetch_quote(provider, self.cfg)
        except Exception as exc:
            provider.record_failure(exc, self.cfg)
            raise
        else:
            provider.record_success(time.perf_counter() - started, self.cfg)
            return quote

    def _candidates(self) -> list[Provider]:
        now = time.monotonic()
        ordered = sorted(self.providers, key=Provider.ranking)

        acquired: list[Provider] = []
        for provider in ordered:
            if provider.try_acquire(now, self.cfg):
                acquired.append(provider)
                if len(acquired) >= self.cfg.parallel_requests:
                    break
        return acquired

    def fetch_once(self) -> GasQuote:
        candidates = self._candidates()
        if not candidates:
            disabled = [p for p in self.providers if p.state is CircuitState.DISABLED]
            if len(disabled) == len(self.providers):
                detail = "; ".join(
                    f"{p.safe_url}: {p.last_error or 'disabled'}" for p in disabled
                )
                raise NoUsableProviderError(f"All RPC providers are disabled: {detail}")

            next_retry = min(
                (
                    max(
                        0.0,
                        p.opened_at
                        + self.cfg.cooldown_seconds
                        - time.monotonic(),
                    )
                    for p in self.providers
                    if p.state is CircuitState.OPEN
                ),
                default=self.cfg.cooldown_seconds,
            )
            raise RpcError(
                f"No RPC provider is currently available; next half-open probe "
                f"in about {next_retry:.1f}s"
            )

        futures: dict[Future[GasQuote], Provider] = {
            self.executor.submit(self._run_provider, provider): provider
            for provider in candidates
        }
        pending: set[Future[GasQuote]] = set(futures)
        errors: list[str] = []

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)

            for future in done:
                provider = futures[future]
                try:
                    quote = future.result()
                except Exception as exc:
                    errors.append(
                        f"{provider.safe_url}: {type(exc).__name__}: {exc}"
                    )
                    self.logger.debug(
                        "RPC failed | provider=%s | error=%s",
                        provider.safe_url,
                        exc,
                    )
                    continue

                # Running HTTP calls cannot reliably be cancelled. They may finish
                # in the background and update their own provider health.
                for other in pending:
                    if other.cancel():
                        futures[other].release_unstarted()

                self.logger.debug(
                    "RPC selected | provider=%s | latency=%dms | block=%s",
                    quote.provider,
                    quote.latency_ms,
                    quote.block_number,
                )
                return quote

        detail = "; ".join(errors[-3:]) or "unknown error"
        raise RpcError(f"All selected RPC providers failed: {detail}")

    def fetch_with_retries(self, stop_event: Optional[threading.Event] = None) -> GasQuote:
        delay = self.cfg.retry_base_delay
        last_error: Optional[BaseException] = None

        for round_number in range(1, self.cfg.retry_rounds + 1):
            try:
                return self.fetch_once()
            except NoUsableProviderError:
                raise
            except TRANSIENT_ERRORS + (RpcError,) as exc:
                last_error = exc
                if round_number >= self.cfg.retry_rounds:
                    break

                upper = max(self.cfg.retry_base_delay, delay * 3.0)
                delay = min(
                    random.uniform(self.cfg.retry_base_delay, upper),
                    self.cfg.retry_max_delay,
                )
                self.logger.warning(
                    "Gas fetch round %d/%d failed; retrying in %.2fs: %s",
                    round_number,
                    self.cfg.retry_rounds,
                    delay,
                    exc,
                )
                if stop_event is not None:
                    if stop_event.wait(delay):
                        raise RpcError("Gas fetch interrupted by shutdown") from exc
                else:
                    time.sleep(delay)

        raise RpcError(
            f"Gas fetch failed after {self.cfg.retry_rounds} round(s): {last_error}"
        ) from last_error

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        for provider in self.providers:
            provider.close()


# ============================================================
# SHUTDOWN / OUTPUT / MAIN
# ============================================================


class GracefulShutdown:
    def __init__(self, logger: logging.Logger) -> None:
        self.event = threading.Event()
        self.logger = logger

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, self._handle)
            except (ValueError, OSError):
                pass

    def _handle(self, signum: int, _frame: Any) -> None:
        self.logger.info("Shutdown signal received: %s", signum)
        self.event.set()

    @property
    def stopped(self) -> bool:
        return self.event.is_set()

    def wait(self, timeout: float) -> bool:
        return self.event.wait(max(0.0, timeout))


def emit(quote: GasQuote, cfg: Config, logger: logging.Logger) -> None:
    data = quote.as_dict()

    if cfg.output_json:
        print(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        return

    if quote.tx_type == "eip1559":
        logger.info(
            "Gas %s gwei | base %s | tip %s | block %s | %dms | %s",
            data["max_fee_gwei"],
            data["base_fee_gwei"],
            data["priority_fee_gwei"],
            quote.block_number,
            quote.latency_ms,
            quote.provider,
        )
    else:
        logger.info(
            "Gas %s gwei | legacy | block %s | %dms | %s",
            data["gas_price_gwei"],
            quote.block_number,
            quote.latency_ms,
            quote.provider,
        )


def monitor(cfg: Config, logger: logging.Logger) -> None:
    shutdown = GracefulShutdown(logger)
    pool = RpcPool(cfg, logger)

    logger.info(
        "Gas monitor started | interval=%.2fs | block_tag=%s",
        cfg.monitor_interval,
        cfg.block_tag,
    )

    next_run = time.monotonic()
    try:
        while not shutdown.stopped:
            try:
                emit(pool.fetch_with_retries(shutdown.event), cfg, logger)
            except Exception as exc:
                logger.error("Gas fetch failed: %s", exc, exc_info=True)

            next_run += cfg.monitor_interval
            now = time.monotonic()

            # If a cycle took too long, skip missed slots instead of running a burst.
            if next_run <= now:
                missed = math.floor((now - next_run) / cfg.monitor_interval) + 1
                next_run += missed * cfg.monitor_interval

            shutdown.wait(next_run - time.monotonic())
    finally:
        pool.close()
        logger.info("Gas monitor stopped")


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logger = setup_logger(cfg)

    try:
        monitor(cfg, logger)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
