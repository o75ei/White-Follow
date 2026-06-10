#!/usr/bin/env python3
"""
White Follow SMM Panel — 100% OOP Flask backend for Railway PostgreSQL.
Gunicorn entrypoint: gunicorn app-9-2-2-1-1-1:app
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from functools import wraps
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, make_response, request, send_from_directory
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash

# ─────────────────────────────────────────────────────────────────────────────
# Module constants
# ─────────────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_ADMIN_PIN = "147258"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("whitefollow")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# ─────────────────────────────────────────────────────────────────────────────
# AppConfig
# ─────────────────────────────────────────────────────────────────────────────


class AppConfig:
    """Central configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.base_dir: str = BASE_DIR
        self.database_url: str = (
            os.environ.get("SUPABASE_DB_URL")
            or os.environ.get("DATABASE_URL")
            or ""
        )
        self.secret_key: str = os.environ.get("SECRET_KEY", secrets.token_hex(32))
        self.darkfollow_api_url: str = os.environ.get(
            "DARKFOLLOW_API_URL", "https://darkfollow.shop/api/v2"
        ).rstrip("/")
        self.darkfollow_api_key: str = os.environ.get("DARKFOLLOW_API_KEY", "")
        self.provider_name: str = "Dark Follow"
        self.cron_interval_seconds: int = int(os.environ.get("CRON_INTERVAL", "90"))
        self.admin_session_hours: int = int(os.environ.get("ADMIN_SESSION_HOURS", "24"))
        self.user_session_days: int = int(os.environ.get("USER_SESSION_DAYS", "30"))
        self.rapid_order_window: int = int(os.environ.get("RAPID_ORDER_WINDOW", "60"))
        self.rapid_order_limit: int = int(os.environ.get("RAPID_ORDER_LIMIT", "8"))
        self.balance_jump_threshold: float = float(
            os.environ.get("BALANCE_JUMP_THRESHOLD", "500")
        )


# ─────────────────────────────────────────────────────────────────────────────
# DatabaseManager — psycopg2 + RealDictCursor wrapper
# ─────────────────────────────────────────────────────────────────────────────


class DatabaseManager:
    """Thread-safe PostgreSQL access with dict rows."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._local = threading.local()

    def _connect(self) -> psycopg2.extensions.connection:
        if not self.config.database_url:
            raise RuntimeError("DATABASE_URL / SUPABASE_DB_URL is not configured")
        conn = psycopg2.connect(self.config.database_url)
        conn.autocommit = False
        return conn

    @contextmanager
    def connection(self) -> Generator[psycopg2.extensions.connection, None, None]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def cursor(
        self, conn: Optional[psycopg2.extensions.connection] = None
    ) -> Generator[psycopg2.extras.RealDictCursor, None, None]:
        own_conn = conn is None
        if own_conn:
            conn = self._connect()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if own_conn:
                conn.commit()
        except Exception:
            if own_conn:
                conn.rollback()
            raise
        finally:
            cur.close()
            if own_conn:
                conn.close()

    def execute(
        self,
        sql: str,
        params: Optional[Tuple[Any, ...] | List[Any]] = None,
        *,
        fetchone: bool = False,
        fetchall: bool = False,
    ) -> Any:
        with self.connection() as conn:
            with self.cursor(conn) as cur:
                cur.execute(sql, params or ())
                if fetchone:
                    return cur.fetchone()
                if fetchall:
                    return cur.fetchall()
                return None

    def ping(self) -> bool:
        try:
            row = self.execute("SELECT 1 AS ok", fetchone=True)
            return bool(row and row.get("ok") == 1)
        except Exception as exc:
            log.warning("Database ping failed: %s", exc)
            return False


# ─────────────────────────────────────────────────────────────────────────────
# SchemaManager
# ─────────────────────────────────────────────────────────────────────────────


class SchemaManager:
    """Creates schema, enforces constraints, deduplicates categories."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def initialize(self) -> None:
        self._create_tables()
        self._deduplicate_categories()
        log.info("Schema initialized")

    def _create_tables(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS users (
            id              SERIAL PRIMARY KEY,
            uid             VARCHAR(32) UNIQUE NOT NULL,
            email           VARCHAR(255) UNIQUE,
            username        VARCHAR(120),
            password_hash   VARCHAR(255),
            balance         NUMERIC(14,4) NOT NULL DEFAULT 0,
            token           VARCHAR(128),
            telegram_id     BIGINT UNIQUE,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS providers (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(120) UNIQUE NOT NULL,
            base_url        TEXT NOT NULL,
            api_key         TEXT,
            balance         NUMERIC(14,4) DEFAULT 0,
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            last_sync       TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS categories (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(255),
            name_ar         VARCHAR(255),
            icon            VARCHAR(64) DEFAULT '📦',
            sort_order      INTEGER NOT NULL DEFAULT 0,
            markup_type     VARCHAR(16) DEFAULT 'percent',
            markup_value    NUMERIC(10,4) DEFAULT 0,
            provider_name   VARCHAR(120),
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS services (
            id              SERIAL PRIMARY KEY,
            category_id     INTEGER REFERENCES categories(id) ON DELETE SET NULL,
            provider_id     INTEGER REFERENCES providers(id) ON DELETE SET NULL,
            remote_id       VARCHAR(64),
            name            VARCHAR(512),
            name_ar         VARCHAR(512),
            rate            NUMERIC(14,6) NOT NULL DEFAULT 0,
            cost            NUMERIC(14,6) NOT NULL DEFAULT 0,
            min_qty         INTEGER NOT NULL DEFAULT 1,
            max_qty         INTEGER NOT NULL DEFAULT 100000,
            service_type    VARCHAR(64) DEFAULT 'default',
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(provider_id, remote_id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
            service_id      INTEGER REFERENCES services(id) ON DELETE SET NULL,
            remote_order_id VARCHAR(64),
            link            TEXT NOT NULL,
            quantity        INTEGER NOT NULL,
            price           NUMERIC(14,4) NOT NULL DEFAULT 0,
            cost            NUMERIC(14,4) NOT NULL DEFAULT 0,
            status          VARCHAR(32) NOT NULL DEFAULT 'pending',
            provider_status TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS payments (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
            amount          NUMERIC(14,4) NOT NULL,
            method          VARCHAR(64) DEFAULT 'manual',
            note            TEXT,
            status          VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS settings (
            key             VARCHAR(128) PRIMARY KEY,
            value           TEXT,
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS security_log (
            id              SERIAL PRIMARY KEY,
            event_type      VARCHAR(64) NOT NULL,
            details         JSONB DEFAULT '{}'::jsonb,
            ip_address      VARCHAR(64),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS cron_log (
            id              SERIAL PRIMARY KEY,
            action          VARCHAR(64) NOT NULL,
            details         TEXT,
            duration_ms     INTEGER DEFAULT 0,
            success         BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS admin_sessions (
            id              SERIAL PRIMARY KEY,
            token           VARCHAR(128) UNIQUE NOT NULL,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER REFERENCES users(id) ON DELETE CASCADE,
            subject         VARCHAR(255) NOT NULL,
            message         TEXT,
            status          VARCHAR(32) NOT NULL DEFAULT 'open',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS payment_gateways (
            id              SERIAL PRIMARY KEY,
            name            VARCHAR(120) NOT NULL,
            active          BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order      INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);
        CREATE INDEX IF NOT EXISTS idx_users_uid ON users(uid);
        CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_security_log_type ON security_log(event_type);
        CREATE INDEX IF NOT EXISTS idx_cron_log_created ON cron_log(created_at DESC);
        """
        with self.db.connection() as conn:
            with self.db.cursor(conn) as cur:
                cur.execute(ddl)
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_categories_name_ar_norm
                    ON categories (LOWER(TRIM(name_ar)))
                    WHERE name_ar IS NOT NULL AND TRIM(name_ar) <> ''
                    """
                )

    def _deduplicate_categories(self) -> None:
        with self.db.connection() as conn:
            with self.db.cursor(conn) as cur:
                cur.execute(
                    """
                    SELECT LOWER(TRIM(name_ar)) AS norm, array_agg(id ORDER BY id) AS ids
                    FROM categories
                    WHERE name_ar IS NOT NULL AND TRIM(name_ar) <> ''
                    GROUP BY LOWER(TRIM(name_ar))
                    HAVING COUNT(*) > 1
                    """
                )
                dup_groups = cur.fetchall() or []
                for group in dup_groups:
                    ids = list(group["ids"])
                    keeper = ids[0]
                    duplicates = ids[1:]
                    for dup_id in duplicates:
                        cur.execute(
                            "UPDATE services SET category_id = %s WHERE category_id = %s",
                            (keeper, dup_id),
                        )
                        cur.execute("DELETE FROM categories WHERE id = %s", (dup_id,))
                if dup_groups:
                    log.info("Removed %s duplicate category groups", len(dup_groups))


# ─────────────────────────────────────────────────────────────────────────────
# SecurityMonitor
# ─────────────────────────────────────────────────────────────────────────────


class SecurityMonitor:
    """Fraud and abuse event logging."""

    def __init__(self, db: DatabaseManager, config: AppConfig) -> None:
        self.db = db
        self.config = config
        self._order_tracker: Dict[int, List[float]] = {}
        self._lock = threading.Lock()

    def _client_ip(self) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr or "unknown"

    def log_event(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.db.execute(
                """
                INSERT INTO security_log (event_type, details, ip_address)
                VALUES (%s, %s::jsonb, %s)
                """,
                (event_type, json.dumps(details or {}), self._client_ip()),
            )
        except Exception as exc:
            log.warning("security_log write failed: %s", exc)

    def record_admin_login_fail(self, pin_attempt: str) -> None:
        self.log_event(
            "admin_login_fail",
            {"pin_length": len(pin_attempt or ""), "timestamp": time.time()},
        )

    def record_balance_jump(
        self, user_id: int, uid: str, old_balance: float, new_balance: float, actor: str
    ) -> None:
        delta = abs(float(new_balance) - float(old_balance))
        if delta >= 0.01:
            self.log_event(
                "balance_jump",
                {
                    "user_id": user_id,
                    "uid": uid,
                    "old_balance": old_balance,
                    "new_balance": new_balance,
                    "delta": delta,
                    "actor": actor,
                },
            )

    def check_rapid_order(self, user_id: int) -> bool:
        now = time.time()
        window = self.config.rapid_order_window
        limit = self.config.rapid_order_limit
        with self._lock:
            history = self._order_tracker.setdefault(user_id, [])
            history[:] = [t for t in history if now - t < window]
            history.append(now)
            if len(history) > limit:
                self.log_event(
                    "rapid_order",
                    {"user_id": user_id, "count_window": len(history), "window_sec": window},
                )
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# PriceCalculator
# ─────────────────────────────────────────────────────────────────────────────


class PriceCalculator:
    """Selling price from provider cost + markup rules."""

    @staticmethod
    def apply_markup(
        base_cost: Decimal,
        markup_type: str,
        markup_value: Decimal,
    ) -> Decimal:
        base = Decimal(str(base_cost))
        mtype = (markup_type or "percent").lower()
        mval = Decimal(str(markup_value or 0))
        if mtype == "fixed":
            result = base + mval
        else:
            result = base * (Decimal("1") + (mval / Decimal("100")))
        return result.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def order_total(rate_per_thousand: Decimal, quantity: int) -> Decimal:
        qty = Decimal(str(max(quantity, 0)))
        rate = Decimal(str(rate_per_thousand))
        total = (rate * qty) / Decimal("1000")
        return total.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


# ─────────────────────────────────────────────────────────────────────────────
# MarkupService
# ─────────────────────────────────────────────────────────────────────────────


class MarkupService:
    """Global and per-category margin configuration."""

    def __init__(self, db: DatabaseManager, calculator: PriceCalculator) -> None:
        self.db = db
        self.calculator = calculator

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key = %s", (key,), fetchone=True
        )
        if row and row.get("value") is not None:
            return str(row["value"])
        return default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )

    def get_global_markup(self) -> Dict[str, Any]:
        return {
            "type": self.get_setting("global_markup_type", "percent"),
            "value": float(self.get_setting("global_markup_value", "0") or 0),
        }

    def set_global_markup(self, markup_type: str, markup_value: float) -> None:
        self.set_setting("global_markup_type", markup_type or "percent")
        self.set_setting("global_markup_value", str(markup_value))

    def resolve_markup_for_category(self, category_id: Optional[int]) -> Tuple[str, Decimal]:
        global_type = self.get_setting("global_markup_type", "percent")
        global_value = Decimal(self.get_setting("global_markup_value", "0") or "0")
        if not category_id:
            return global_type, global_value
        row = self.db.execute(
            "SELECT markup_type, markup_value FROM categories WHERE id = %s",
            (category_id,),
            fetchone=True,
        )
        if not row:
            return global_type, global_value
        cat_type = row.get("markup_type")
        cat_value = row.get("markup_value")
        if cat_value is not None and Decimal(str(cat_value)) != 0:
            return str(cat_type or global_type), Decimal(str(cat_value))
        return global_type, global_value

    def bulk_category_margin(self, markup_type: str, markup_value: float) -> int:
        self.set_global_markup(markup_type, markup_value)
        rows = self.db.execute(
            """
            UPDATE categories
            SET markup_type = %s, markup_value = %s
            RETURNING id
            """,
            (markup_type, markup_value),
            fetchall=True,
        )
        return len(rows or [])

    def apply_panel_margins(
        self, global_percent: float, categories: List[Dict[str, Any]]
    ) -> int:
        """Apply global percent + per-category sliders from admin panel."""
        self.set_global_markup("percent", global_percent)
        updated = 0
        for cat in categories or []:
            cid = cat.get("id")
            if cid is None:
                continue
            mtype = str(cat.get("markup_type") or "percent")
            mval = float(cat.get("markup_value") or 0)
            if self.set_category_markup(int(cid), mtype, mval):
                updated += 1
        return updated

    def set_category_markup(self, category_id: int, markup_type: str, markup_value: float) -> bool:
        row = self.db.execute(
            """
            UPDATE categories
            SET markup_type = %s, markup_value = %s
            WHERE id = %s
            RETURNING id
            """,
            (markup_type, markup_value, category_id),
            fetchone=True,
        )
        return bool(row)

    def recalculate_all_service_rates(self) -> int:
        services = self.db.execute(
            """
            SELECT s.id, s.cost, s.category_id
            FROM services s
            WHERE s.active = TRUE
            """,
            fetchall=True,
        ) or []
        updated = 0
        for svc in services:
            mtype, mval = self.resolve_markup_for_category(svc.get("category_id"))
            new_rate = self.calculator.apply_markup(
                Decimal(str(svc.get("cost") or 0)), mtype, mval
            )
            self.db.execute(
                "UPDATE services SET rate = %s, updated_at = NOW() WHERE id = %s",
                (float(new_rate), svc["id"]),
            )
            updated += 1
        return updated


# ─────────────────────────────────────────────────────────────────────────────
# DarkFollowClient
# ─────────────────────────────────────────────────────────────────────────────


class DarkFollowClient:
    """Dark Follow SMM API client with resilient balance fetching."""

    def __init__(self, db: DatabaseManager, config: AppConfig) -> None:
        self.db = db
        self.config = config
        self._last_good_balance: Optional[float] = None
        self._last_good_at: float = 0.0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "WhiteFollow/9.2.2"})

    def ensure_provider_registered(self) -> int:
        row = self.db.execute(
            "SELECT id, api_key, base_url FROM providers WHERE name = %s",
            (self.config.provider_name,),
            fetchone=True,
        )
        if row:
            api_key = row.get("api_key") or self.config.darkfollow_api_key
            base_url = row.get("base_url") or self.config.darkfollow_api_url
            if api_key and api_key != row.get("api_key"):
                self.db.execute(
                    "UPDATE providers SET api_key = %s, base_url = %s WHERE id = %s",
                    (api_key, base_url, row["id"]),
                )
            return int(row["id"])
        inserted = self.db.execute(
            """
            INSERT INTO providers (name, base_url, api_key, active)
            VALUES (%s, %s, %s, TRUE)
            RETURNING id
            """,
            (
                self.config.provider_name,
                self.config.darkfollow_api_url,
                self.config.darkfollow_api_key,
            ),
            fetchone=True,
        )
        return int(inserted["id"])

    def _collect_api_keys(self) -> List[str]:
        keys: List[str] = []
        if self.config.darkfollow_api_key:
            keys.append(self.config.darkfollow_api_key.strip())
        row = self.db.execute(
            "SELECT api_key FROM providers WHERE name = %s AND active = TRUE",
            (self.config.provider_name,),
            fetchone=True,
        )
        if row and row.get("api_key"):
            db_key = str(row["api_key"]).strip()
            if db_key and db_key not in keys:
                keys.append(db_key)
        return keys

    def _provider_row(self) -> Optional[Dict[str, Any]]:
        return self.db.execute(
            "SELECT * FROM providers WHERE name = %s",
            (self.config.provider_name,),
            fetchone=True,
        )

    def _request(self, params: Dict[str, str], timeout: int = 25) -> Tuple[bool, Any, str]:
        provider = self._provider_row()
        base_url = (
            (provider or {}).get("base_url")
            or self.config.darkfollow_api_url
        )
        keys = self._collect_api_keys()
        if not keys:
            return False, None, "no API key configured"
        last_error = "unknown"
        for key in keys:
            try:
                query = dict(params)
                query["key"] = key
                resp = self._session.get(base_url, params=query, timeout=timeout)
                if resp.status_code == 401:
                    last_error = "401 unauthorized"
                    continue
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}"
                    continue
                try:
                    data = resp.json()
                except ValueError:
                    data = {"raw": resp.text[:500]}
                return True, data, ""
            except requests.RequestException as exc:
                last_error = str(exc)
        return False, None, last_error

    def fetch_balance(self) -> Dict[str, Any]:
        with self._lock:
            ok, data, err = self._request({"action": "balance"})
            balance: Optional[float] = None
            source = "live"
            if ok and isinstance(data, dict):
                for field in ("balance", "wallet", "funds"):
                    if field in data:
                        try:
                            balance = float(data[field])
                            break
                        except (TypeError, ValueError):
                            pass
                if balance is None and "data" in data:
                    try:
                        balance = float(data["data"])
                    except (TypeError, ValueError):
                        pass
            if balance is not None:
                self._last_good_balance = balance
                self._last_good_at = time.time()
                try:
                    self.db.execute(
                        "UPDATE providers SET balance = %s, last_sync = NOW() WHERE name = %s",
                        (balance, self.config.provider_name),
                    )
                except Exception as exc:
                    log.warning("provider balance persist failed: %s", exc)
                return {
                    "ok": True,
                    "balance": balance,
                    "source": source,
                    "cached": False,
                    "error": None,
                }
            if self._last_good_balance is not None:
                return {
                    "ok": True,
                    "balance": self._last_good_balance,
                    "source": "cache",
                    "cached": True,
                    "error": err or "live fetch failed",
                }
            provider = self._provider_row()
            if provider and provider.get("balance") is not None:
                try:
                    db_balance = float(provider["balance"])
                    self._last_good_balance = db_balance
                    return {
                        "ok": True,
                        "balance": db_balance,
                        "source": "database",
                        "cached": True,
                        "error": err,
                    }
                except (TypeError, ValueError):
                    pass
            return {
                "ok": True,
                "balance": 0.0,
                "source": "fallback",
                "cached": False,
                "error": err or "balance unavailable",
            }

    def fetch_services(self) -> Tuple[bool, List[Dict[str, Any]], str]:
        ok, data, err = self._request({"action": "services"})
        if not ok:
            return False, [], err
        if isinstance(data, list):
            return True, data, ""
        if isinstance(data, dict):
            for key in ("services", "data", "result"):
                if key in data and isinstance(data[key], list):
                    return True, data[key], ""
        return False, [], "unexpected services payload"

    def place_order(self, remote_service_id: str, link: str, quantity: int) -> Tuple[bool, Dict[str, Any], str]:
        ok, data, err = self._request(
            {
                "action": "add",
                "service": str(remote_service_id),
                "link": link,
                "quantity": str(quantity),
            }
        )
        if not ok:
            return False, {}, err
        if not isinstance(data, dict):
            return False, {}, "invalid order response"
        return True, data, ""

    def order_status(self, remote_order_id: str) -> Tuple[bool, str, str]:
        ok, data, err = self._request({"action": "status", "order": str(remote_order_id)})
        if not ok:
            return False, "unknown", err
        if isinstance(data, dict):
            status = str(data.get("status") or data.get("order_status") or "unknown")
            return True, status, ""
        return False, "unknown", "invalid status response"

    def ping_speed(self) -> Dict[str, Any]:
        started = time.perf_counter()
        ok, data, err = self._request({"action": "balance"}, timeout=15)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "success": ok,
            "latency_ms": elapsed_ms,
            "error": err or None,
            "sample": data if ok else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# CatalogManager
# ─────────────────────────────────────────────────────────────────────────────


class CatalogManager:
    """Catalog sync, deduplication, and storefront assembly."""

    _CAT_SPLIT_RE = re.compile(r"\s*\|.*$")

    def __init__(
        self,
        db: DatabaseManager,
        darkfollow: DarkFollowClient,
        markup: MarkupService,
        calculator: PriceCalculator,
        schema: SchemaManager,
    ) -> None:
        self.db = db
        self.darkfollow = darkfollow
        self.markup = markup
        self.calculator = calculator
        self.schema = schema

    @classmethod
    def normalize_category_name(cls, name: str) -> str:
        s = (name or "").strip().lower()
        s = cls._CAT_SPLIT_RE.sub("", s).strip()
        s = re.sub(r"\s+", " ", s)
        return s

    def deduplicate_database(self) -> None:
        self.schema._deduplicate_categories()

    def sync_from_provider(self) -> Dict[str, Any]:
        provider_id = self.darkfollow.ensure_provider_registered()
        ok, remote_services, err = self.darkfollow.fetch_services()
        if not ok:
            return {"ok": False, "error": err, "synced": 0}
        categories_seen: Dict[str, int] = {}
        synced = 0
        remote_ids: set = set()
        with self.db.connection() as conn:
            with self.db.cursor(conn) as cur:
                for item in remote_services:
                    if not isinstance(item, dict):
                        continue
                    remote_id = str(item.get("service") or item.get("id") or "")
                    if not remote_id:
                        continue
                    remote_ids.add(remote_id)
                    cat_name = str(item.get("category") or item.get("type") or "General")
                    norm = self.normalize_category_name(cat_name)
                    if norm not in categories_seen:
                        cur.execute(
                            """
                            SELECT id FROM categories
                            WHERE LOWER(TRIM(name_ar)) = LOWER(TRIM(%s))
                            LIMIT 1
                            """,
                            (cat_name,),
                        )
                        found = cur.fetchone()
                        if found:
                            cat_id = found["id"]
                        else:
                            cur.execute(
                                """
                                INSERT INTO categories (name, name_ar, sort_order, provider_name, active)
                                VALUES (%s, %s, %s, %s, TRUE)
                                RETURNING id
                                """,
                                (
                                    cat_name,
                                    cat_name,
                                    len(categories_seen),
                                    self.darkfollow.config.provider_name,
                                ),
                            )
                            inserted = cur.fetchone()
                            cat_id = inserted["id"] if inserted else None
                        if cat_id:
                            categories_seen[norm] = cat_id
                    cat_id = categories_seen.get(norm)
                    cost = Decimal(str(item.get("rate") or item.get("price") or 0))
                    mtype, mval = self.markup.resolve_markup_for_category(cat_id)
                    sell_rate = self.calculator.apply_markup(cost, mtype, mval)
                    name = str(item.get("name") or item.get("service_name") or remote_id)
                    min_qty = int(item.get("min") or 1)
                    max_qty = int(item.get("max") or 100000)
                    cur.execute(
                        """
                        INSERT INTO services (
                            category_id, provider_id, remote_id, name, name_ar,
                            rate, cost, min_qty, max_qty, service_type, active, updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,NOW())
                        ON CONFLICT (provider_id, remote_id) DO UPDATE SET
                            category_id = EXCLUDED.category_id,
                            name = EXCLUDED.name,
                            name_ar = EXCLUDED.name_ar,
                            cost = EXCLUDED.cost,
                            rate = EXCLUDED.rate,
                            min_qty = EXCLUDED.min_qty,
                            max_qty = EXCLUDED.max_qty,
                            service_type = EXCLUDED.service_type,
                            active = TRUE,
                            updated_at = NOW()
                        """,
                        (
                            cat_id,
                            provider_id,
                            remote_id,
                            name,
                            name,
                            float(sell_rate),
                            float(cost),
                            min_qty,
                            max_qty,
                            str(item.get("type") or "default"),
                        ),
                    )
                    synced += 1
                if remote_ids:
                    cur.execute(
                        """
                        UPDATE services SET active = FALSE, updated_at = NOW()
                        WHERE provider_id = %s AND remote_id NOT IN %s
                        """,
                        (provider_id, tuple(remote_ids)),
                    )
        self.deduplicate_database()
        return {"ok": True, "synced": synced, "remote_count": len(remote_ids)}

    def refresh_rates(self) -> int:
        return self.markup.recalculate_all_service_rates()

    def list_for_storefront(self, lang: str = "ar") -> List[Dict[str, Any]]:
        self.deduplicate_database()
        categories = self.db.execute(
            """
            SELECT id, name, name_ar, icon, sort_order
            FROM categories
            WHERE active = TRUE
            ORDER BY sort_order, id
            """,
            fetchall=True,
        ) or []
        services = self.db.execute(
            """
            SELECT id, category_id, name, name_ar, rate, min_qty AS min, max_qty AS max
            FROM services
            WHERE active = TRUE
            ORDER BY category_id, id
            """,
            fetchall=True,
        ) or []
        by_cat: Dict[int, List[Dict[str, Any]]] = {}
        for svc in services:
            cid = svc.get("category_id")
            if cid is None:
                continue
            by_cat.setdefault(cid, []).append(
                {
                    "id": svc["id"],
                    "name": svc.get("name"),
                    "name_ar": svc.get("name_ar") or svc.get("name"),
                    "rate": float(svc.get("rate") or 0),
                    "min": int(svc.get("min") or 1),
                    "max": int(svc.get("max") or 100000),
                }
            )
        merged: Dict[str, Dict[str, Any]] = {}
        for cat in categories:
            norm = self.normalize_category_name(cat.get("name_ar") or cat.get("name") or "")
            if not norm:
                continue
            payload = {
                "id": cat["id"],
                "name": cat.get("name"),
                "name_ar": cat.get("name_ar") or cat.get("name"),
                "icon": cat.get("icon"),
                "sort_order": cat.get("sort_order"),
                "services": list(by_cat.get(cat["id"], [])),
            }
            if norm not in merged:
                merged[norm] = payload
            else:
                existing_ids = {s["id"] for s in merged[norm]["services"]}
                for svc in payload["services"]:
                    if svc["id"] not in existing_ids:
                        merged[norm]["services"].append(svc)
                        existing_ids.add(svc["id"])
        result = sorted(merged.values(), key=lambda c: (c.get("sort_order") or 0, c.get("id") or 0))
        if lang == "en":
            for cat in result:
                for svc in cat["services"]:
                    if svc.get("name"):
                        svc["name_ar"] = svc["name"]
        return result

    def services_status(self) -> Dict[str, Any]:
        rows = self.db.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE active = TRUE) AS active_services,
                COUNT(*) FILTER (WHERE active = FALSE) AS inactive_services,
                COUNT(DISTINCT category_id) AS categories_used
            FROM services
            """,
            fetchone=True,
        ) or {}
        cats = self.db.execute("SELECT COUNT(*) AS c FROM categories WHERE active = TRUE", fetchone=True)
        return {
            "active_services": int(rows.get("active_services") or 0),
            "inactive_services": int(rows.get("inactive_services") or 0),
            "categories_used": int(rows.get("categories_used") or 0),
            "active_categories": int((cats or {}).get("c") or 0),
        }

    def list_categories_admin(self) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT c.id, c.name, c.name_ar, c.icon, c.sort_order,
                   c.markup_type, c.markup_value,
                   COUNT(s.id) AS service_count
            FROM categories c
            LEFT JOIN services s ON s.category_id = c.id AND s.active = TRUE
            WHERE c.active = TRUE
            GROUP BY c.id
            ORDER BY c.sort_order, c.id
            """,
            fetchall=True,
        ) or []
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "name": row.get("name"),
                    "name_ar": row.get("name_ar"),
                    "icon": row.get("icon"),
                    "sort_order": row.get("sort_order"),
                    "markup_type": row.get("markup_type"),
                    "markup_value": float(row.get("markup_value") or 0),
                    "service_count": int(row.get("service_count") or 0),
                }
            )
        return out


# ─────────────────────────────────────────────────────────────────────────────
# UserService
# ─────────────────────────────────────────────────────────────────────────────


class UserService:
    """Registration, authentication, balance, and orders."""

    def __init__(
        self,
        db: DatabaseManager,
        config: AppConfig,
        security: SecurityMonitor,
        calculator: PriceCalculator,
        darkfollow: DarkFollowClient,
    ) -> None:
        self.db = db
        self.config = config
        self.security = security
        self.calculator = calculator
        self.darkfollow = darkfollow

    @staticmethod
    def generate_uid() -> str:
        return f"WF-{secrets.token_hex(6).upper()}"

    def register(self, email: str, username: str, password: str) -> Dict[str, Any]:
        email = (email or "").strip().lower()
        username = (username or "").strip()
        if not email or not password:
            return {"ok": False, "error": "email and password required"}
        existing = self.db.execute(
            "SELECT id FROM users WHERE LOWER(email) = %s", (email,), fetchone=True
        )
        if existing:
            return {"ok": False, "error": "email already registered"}
        uid = self.generate_uid()
        token = secrets.token_urlsafe(32)
        pwd_hash = generate_password_hash(password)
        row = self.db.execute(
            """
            INSERT INTO users (uid, email, username, password_hash, balance, token)
            VALUES (%s, %s, %s, %s, 0, %s)
            RETURNING id, uid, email, username, balance
            """,
            (uid, email, username or email.split("@")[0], pwd_hash, token),
            fetchone=True,
        )
        return {
            "ok": True,
            "token": token,
            "user": self.serialize_user(row),
            "message": "registration successful",
        }

    def login(self, email: str, password: str) -> Dict[str, Any]:
        email = (email or "").strip().lower()
        row = self.db.execute(
            """
            SELECT id, uid, email, username, password_hash, balance, is_active
            FROM users WHERE LOWER(email) = %s OR uid = %s OR username = %s
            LIMIT 1
            """,
            (email, email.upper(), email),
            fetchone=True,
        )
        if not row or not row.get("password_hash"):
            return {"ok": False, "error": "invalid credentials"}
        if not row.get("is_active", True):
            return {"ok": False, "error": "account disabled"}
        if not check_password_hash(row["password_hash"], password or ""):
            return {"ok": False, "error": "invalid credentials"}
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "UPDATE users SET token = %s, updated_at = NOW() WHERE id = %s",
            (token, row["id"]),
        )
        row["balance"] = row.get("balance")
        return {"ok": True, "token": token, "user": self.serialize_user(row)}

    def logout(self, token: str) -> None:
        if token:
            self.db.execute(
                "UPDATE users SET token = NULL, updated_at = NOW() WHERE token = %s",
                (token,),
            )

    def user_from_token(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        return self.db.execute(
            """
            SELECT id, uid, email, username, balance, is_active
            FROM users WHERE token = %s AND is_active = TRUE
            LIMIT 1
            """,
            (token.strip(),),
            fetchone=True,
        )

    @staticmethod
    def serialize_user(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not row:
            return {}
        return {
            "id": row.get("id"),
            "uid": row.get("uid"),
            "email": row.get("email"),
            "username": row.get("username"),
            "balance": float(row.get("balance") or 0),
        }

    def find_user(self, query: str) -> Optional[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return None
        row = self.db.execute(
            """
            SELECT id, uid, email, username, balance, created_at
            FROM users
            WHERE uid ILIKE %s OR LOWER(email) LIKE LOWER(%s) OR username ILIKE %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (q, f"%{q}%", f"%{q}%"),
            fetchone=True,
        )
        return row

    def list_users(self, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT id, uid, email, username, balance, created_at, is_active
            FROM users
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
            fetchall=True,
        ) or []
        return [
            {
                "id": r["id"],
                "uid": r.get("uid"),
                "email": r.get("email"),
                "username": r.get("username"),
                "balance": float(r.get("balance") or 0),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
                "is_active": bool(r.get("is_active", True)),
            }
            for r in rows
        ]

    def adjust_balance(
        self,
        user_id: int,
        amount: float,
        *,
        mode: str = "add",
        actor: str = "admin",
    ) -> Dict[str, Any]:
        row = self.db.execute(
            "SELECT id, uid, balance FROM users WHERE id = %s",
            (user_id,),
            fetchone=True,
        )
        if not row:
            return {"ok": False, "error": "user not found"}
        old_balance = float(row.get("balance") or 0)
        if mode == "set":
            new_balance = float(amount)
        else:
            new_balance = old_balance + float(amount)
        if new_balance < 0:
            return {"ok": False, "error": "balance cannot be negative"}
        self.db.execute(
            "UPDATE users SET balance = %s, updated_at = NOW() WHERE id = %s",
            (new_balance, user_id),
        )
        if abs(new_balance - old_balance) >= self.config.balance_jump_threshold:
            self.security.record_balance_jump(
                user_id, str(row.get("uid")), old_balance, new_balance, actor
            )
        return {
            "ok": True,
            "user_id": user_id,
            "uid": row.get("uid"),
            "old_balance": old_balance,
            "new_balance": new_balance,
        }

    def topup_by_uid(self, uid: str, amount: float) -> Dict[str, Any]:
        uid = (uid or "").strip().upper()
        row = self.db.execute(
            "SELECT id FROM users WHERE UPPER(uid) = %s",
            (uid,),
            fetchone=True,
        )
        if not row:
            return {"ok": False, "error": "user not found"}
        return self.adjust_balance(int(row["id"]), amount, mode="add", actor="admin_uid_topup")

    def place_order(
        self,
        user: Dict[str, Any],
        service_id: int,
        link: str,
        quantity: int,
    ) -> Dict[str, Any]:
        if self.security.check_rapid_order(int(user["id"])):
            return {"ok": False, "error": "too many orders — please wait"}
        svc = self.db.execute(
            """
            SELECT s.*, pr.id AS provider_ref
            FROM services s
            LEFT JOIN providers pr ON pr.id = s.provider_id
            WHERE s.id = %s AND s.active = TRUE
            """,
            (service_id,),
            fetchone=True,
        )
        if not svc:
            return {"ok": False, "error": "service not found"}
        qty = int(quantity)
        min_q = int(svc.get("min_qty") or 1)
        max_q = int(svc.get("max_qty") or 100000)
        if qty < min_q or qty > max_q:
            return {"ok": False, "error": f"quantity must be between {min_q} and {max_q}"}
        if not (link or "").strip():
            return {"ok": False, "error": "link required"}
        price = self.calculator.order_total(Decimal(str(svc.get("rate") or 0)), qty)
        cost = self.calculator.order_total(Decimal(str(svc.get("cost") or 0)), qty)
        balance = Decimal(str(user.get("balance") or 0))
        if balance < price:
            return {"ok": False, "error": "insufficient balance"}
        remote_id = str(svc.get("remote_id") or "")
        remote_order_id = None
        provider_status = "queued"
        if remote_id:
            ok, resp, err = self.darkfollow.place_order(remote_id, link.strip(), qty)
            if not ok:
                return {"ok": False, "error": err or "provider rejected order"}
            remote_order_id = str(
                resp.get("order")
                or resp.get("order_id")
                or resp.get("id")
                or ""
            )
            provider_status = str(resp.get("status") or "pending")
        new_balance = balance - price
        order_row = self.db.execute(
            """
            INSERT INTO orders (
                user_id, service_id, remote_order_id, link, quantity,
                price, cost, status, provider_status
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                user["id"],
                service_id,
                remote_order_id,
                link.strip(),
                qty,
                float(price),
                float(cost),
                "pending",
                provider_status,
            ),
            fetchone=True,
        )
        self.db.execute(
            "UPDATE users SET balance = %s, updated_at = NOW() WHERE id = %s",
            (float(new_balance), user["id"]),
        )
        return {"ok": True, "order_id": order_row["id"], "price": float(price)}

    def list_orders(self, user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT o.id, o.status, o.price, o.quantity, o.link, o.created_at,
                   COALESCE(s.name_ar, s.name, '') AS service_name
            FROM orders o
            LEFT JOIN services s ON s.id = o.service_id
            WHERE o.user_id = %s
            ORDER BY o.id DESC
            LIMIT %s
            """,
            (user_id, limit),
            fetchall=True,
        ) or []
        return [
            {
                "id": r["id"],
                "status": r.get("status"),
                "price": float(r.get("price") or 0),
                "quantity": r.get("quantity"),
                "link": r.get("link"),
                "service_name": r.get("service_name"),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────────────────
# AdminAuthService
# ─────────────────────────────────────────────────────────────────────────────


class AdminAuthService:
    """Plain-text master PIN authentication with DB + memory sessions."""

    def __init__(self, db: DatabaseManager, config: AppConfig, security: SecurityMonitor) -> None:
        self.db = db
        self.config = config
        self.security = security
        self._memory_tokens: Dict[str, float] = {}
        self._lock = threading.Lock()

    def login(self, pin: str) -> Dict[str, Any]:
        submitted = str(pin or "").strip()
        if submitted != MASTER_ADMIN_PIN:
            self.security.record_admin_login_fail(submitted)
            return {"ok": False, "error": "incorrect password"}
        token = secrets.token_urlsafe(40)
        expires = datetime.now(timezone.utc) + timedelta(hours=self.config.admin_session_hours)
        try:
            self.db.execute(
                """
                INSERT INTO admin_sessions (token, expires_at)
                VALUES (%s, %s)
                """,
                (token, expires),
            )
        except Exception as exc:
            log.warning("admin session DB insert failed, using memory fallback: %s", exc)
        with self._lock:
            self._memory_tokens[token] = expires.timestamp()
        resp = make_response(
            jsonify({"ok": True, "token": token, "expires_at": expires.isoformat()})
        )
        resp.set_cookie(
            "admin_token",
            token,
            httponly=True,
            samesite="Lax",
            max_age=self.config.admin_session_hours * 3600,
        )
        return resp

    def extract_token(self) -> str:
        return (
            request.headers.get("X-Admin-Token")
            or request.cookies.get("admin_token")
            or ""
        ).strip()

    def verify_token(self, token: str) -> bool:
        if not token:
            return False
        now = datetime.now(timezone.utc)
        with self._lock:
            mem_exp = self._memory_tokens.get(token)
            if mem_exp and mem_exp > time.time():
                return True
            if mem_exp:
                self._memory_tokens.pop(token, None)
        try:
            row = self.db.execute(
                """
                SELECT id FROM admin_sessions
                WHERE token = %s AND expires_at > NOW()
                LIMIT 1
                """,
                (token,),
                fetchone=True,
            )
            return bool(row)
        except Exception as exc:
            log.warning("admin session verify DB error: %s", exc)
            with self._lock:
                return token in self._memory_tokens

    def logout(self, token: str) -> None:
        with self._lock:
            self._memory_tokens.pop(token, None)
        try:
            self.db.execute("DELETE FROM admin_sessions WHERE token = %s", (token,))
        except Exception as exc:
            log.warning("admin logout DB error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# CronSimulator + background order sync
# ─────────────────────────────────────────────────────────────────────────────


class CronSimulator:
    """Cron maintenance, order sync, and simulation previews."""

    def __init__(
        self,
        db: DatabaseManager,
        darkfollow: DarkFollowClient,
        catalog: CatalogManager,
        config: AppConfig,
    ) -> None:
        self.db = db
        self.darkfollow = darkfollow
        self.catalog = catalog
        self.config = config
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _log(self, action: str, details: str, duration_ms: int, success: bool = True) -> None:
        try:
            self.db.execute(
                """
                INSERT INTO cron_log (action, details, duration_ms, success)
                VALUES (%s, %s, %s, %s)
                """,
                (action, details, duration_ms, success),
            )
        except Exception as exc:
            log.warning("cron_log write failed: %s", exc)

    def sync_orders(self) -> Dict[str, Any]:
        started = time.perf_counter()
        rows = self.db.execute(
            """
            SELECT id, remote_order_id, status
            FROM orders
            WHERE remote_order_id IS NOT NULL
              AND remote_order_id <> ''
              AND status NOT IN ('completed', 'canceled', 'cancelled', 'refunded')
            ORDER BY id DESC
            LIMIT 200
            """,
            fetchall=True,
        ) or []
        updated = 0
        for row in rows:
            ok, status, _ = self.darkfollow.order_status(str(row["remote_order_id"]))
            if ok and status:
                mapped = status.lower()
                self.db.execute(
                    """
                    UPDATE orders
                    SET status = %s, provider_status = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (mapped, status, row["id"]),
                )
                updated += 1
        elapsed = int((time.perf_counter() - started) * 1000)
        details = f"synced {updated}/{len(rows)} orders"
        self._log("sync_orders", details, elapsed)
        return {"ok": True, "checked": len(rows), "updated": updated, "duration_ms": elapsed}

    def run_maintenance(self) -> Dict[str, Any]:
        started = time.perf_counter()
        order_result = self.sync_orders()
        elapsed = int((time.perf_counter() - started) * 1000)
        self._log("run_maintenance", json.dumps(order_result), elapsed)
        return {"ok": True, "orders": order_result, "duration_ms": elapsed}

    def simulate(self) -> Dict[str, Any]:
        started = time.perf_counter()
        speed = self.darkfollow.ping_speed()
        pending_orders = self.db.execute(
            """
            SELECT COUNT(*) AS c FROM orders
            WHERE status NOT IN ('completed','canceled','cancelled','refunded')
            """,
            fetchone=True,
        )
        active_services = self.db.execute(
            "SELECT COUNT(*) AS c FROM services WHERE active = TRUE", fetchone=True
        )
        users_count = self.db.execute("SELECT COUNT(*) AS c FROM users", fetchone=True)
        catalog_stats = self.catalog.services_status()
        estimated_queries = (
            int((pending_orders or {}).get("c") or 0)
            + int((active_services or {}).get("c") or 0) // 50
            + 3
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        payload = {
            "ok": True,
            "provider_speed": speed,
            "resource_preview": {
                "pending_orders": int((pending_orders or {}).get("c") or 0),
                "active_services": int((active_services or {}).get("c") or 0),
                "users": int((users_count or {}).get("c") or 0),
                "estimated_db_queries_per_cron": estimated_queries,
                "catalog": catalog_stats,
            },
            "recommendation": (
                "healthy"
                if speed.get("latency_ms", 9999) < 2000
                else "slow_provider"
            ),
            "duration_ms": elapsed,
        }
        self._log("simulate", json.dumps(payload), elapsed)
        return payload

    def list_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT id, action, details, duration_ms, success, created_at
            FROM cron_log
            ORDER BY id DESC
            LIMIT %s
            """,
            (limit,),
            fetchall=True,
        ) or []
        return [
            {
                "id": r["id"],
                "action": r.get("action"),
                "details": r.get("details"),
                "duration_ms": r.get("duration_ms"),
                "success": bool(r.get("success", True)),
                "created_at": r.get("created_at").isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]

    def _loop(self) -> None:
        log.info("Background cron thread started (interval=%ss)", self.config.cron_interval_seconds)
        while not self._stop.is_set():
            try:
                self.run_maintenance()
            except Exception as exc:
                log.exception("cron loop error: %s", exc)
                self._log("cron_error", str(exc), 0, success=False)
            self._stop.wait(self.config.cron_interval_seconds)

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="wf-cron", daemon=True)
        self._thread.start()

    def stop_background(self) -> None:
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
# Route wiring
# ─────────────────────────────────────────────────────────────────────────────


class PanelApplication:
    """Bootstraps services and registers all HTTP routes."""

    def __init__(self, flask_app: Flask, config: AppConfig) -> None:
        self.app = flask_app
        self.config = config
        self.db = DatabaseManager(config)
        self.schema = SchemaManager(self.db)
        self.security = SecurityMonitor(self.db, config)
        self.calculator = PriceCalculator()
        self.markup = MarkupService(self.db, self.calculator)
        self.darkfollow = DarkFollowClient(self.db, config)
        self.catalog = CatalogManager(
            self.db, self.darkfollow, self.markup, self.calculator, self.schema
        )
        self.users = UserService(
            self.db, config, self.security, self.calculator, self.darkfollow
        )
        self.admin_auth = AdminAuthService(self.db, config, self.security)
        self.cron = CronSimulator(self.db, self.darkfollow, self.catalog, config)
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        if self.db.ping():
            self.schema.initialize()
            self.darkfollow.ensure_provider_registered()
            self._seed_defaults()
            self.cron.start_background()
            log.info("PanelApplication initialized with database")
        else:
            log.error("Database unavailable — routes active but persistence disabled")
        self._register_routes()
        self._initialized = True

    def _seed_defaults(self) -> None:
        if not self.markup.get_setting("global_markup_type"):
            self.markup.set_global_markup("percent", 20.0)
        gateways = self.db.execute("SELECT COUNT(*) AS c FROM payment_gateways", fetchone=True)
        if int((gateways or {}).get("c") or 0) == 0:
            self.db.execute(
                """
                INSERT INTO payment_gateways (name, active, sort_order) VALUES
                ('Manual Transfer', TRUE, 1),
                ('Crypto', TRUE, 2)
                """
            )
        if not self.markup.get_setting("faq_ar"):
            self.markup.set_setting(
                "faq_ar",
                json.dumps(
                    [
                        {
                            "q": "كيف أشحن رصيدي؟",
                            "a": "من قسم الشحن اختر البوابة وأرسل طلب التعبئة.",
                        }
                    ],
                    ensure_ascii=False,
                ),
            )
        if not self.markup.get_setting("tos_ar"):
            self.markup.set_setting("tos_ar", "باستخدامك للخدمة فإنك توافق على الشروط والأحكام.")

    # ── decorators ───────────────────────────────────────────────────────────

    def safe_route(self, rule: str, **options: Any) -> Callable:
        methods = options.get("methods")

        def decorator(fn: Callable) -> Callable:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    log.error("Route %s failed: %s\n%s", rule, exc, traceback.format_exc())
                    return (
                        jsonify(
                            {
                                "ok": False,
                                "error": "internal_error",
                                "message": str(exc),
                            }
                        ),
                        200,
                    )

            endpoint = options.pop("endpoint", None)
            self.app.add_url_rule(rule, endpoint, wrapper, methods=methods, **options)
            return wrapper

        return decorator

    def require_admin(self, fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = self.admin_auth.extract_token()
            if not self.admin_auth.verify_token(token):
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return fn(*args, **kwargs)

        return wrapper

    def require_user(self, fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            token = request.headers.get("X-User-Token", "").strip()
            user = self.users.user_from_token(token)
            if not user:
                return jsonify({"ok": False, "error": "unauthorized"}), 401
            return fn(user, *args, **kwargs)

        return wrapper

    def _send_html(self, filenames: Tuple[str, ...]) -> Any:
        for name in filenames:
            path = os.path.join(self.config.base_dir, name)
            if os.path.isfile(path):
                return send_from_directory(self.config.base_dir, name)
        return jsonify({"ok": False, "error": "file not found"}), 404

    # ── routes ───────────────────────────────────────────────────────────────

    def _register_routes(self) -> None:
        sr = self.safe_route
        admin = self.require_admin
        user_auth = self.require_user

        @sr("/health")
        def health() -> Any:
            return jsonify(
                {
                    "ok": True,
                    "database": self.db.ping(),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )

        @sr("/")
        def index_page() -> Any:
            return self._send_html(("index.html",))

        # ── Admin auth ──────────────────────────────────────────────────────

        @sr("/admin/login", methods=["POST"])
        def admin_login() -> Any:
            body = request.get_json(silent=True) or {}
            pin = body.get("pin") or body.get("password") or ""
            result = self.admin_auth.login(str(pin))
            if hasattr(result, "headers"):
                return result
            payload = result if isinstance(result, dict) else {"ok": False, "error": "login failed"}
            code = 200 if payload.get("ok") else 401
            return jsonify(payload), code

        @sr("/admin/verify", methods=["GET"])
        def admin_verify() -> Any:
            token = self.admin_auth.extract_token()
            return jsonify({"ok": self.admin_auth.verify_token(token), "token": token or None})

        @sr("/admin/logout", methods=["POST"])
        @admin
        def admin_logout() -> Any:
            token = self.admin_auth.extract_token()
            self.admin_auth.logout(token)
            resp = make_response(jsonify({"ok": True}))
            resp.delete_cookie("admin_token")
            return resp

        @sr("/admin")
        def admin_panel() -> Any:
            return self._send_html(
                ("admin-panel-4.html", "admin-panel.html", "admin.html")
            )

        # ── Admin dashboard ───────────────────────────────────────────────

        @sr("/admin/stats", methods=["GET"])
        @admin
        def admin_stats() -> Any:
            users = self.db.execute("SELECT COUNT(*) AS c FROM users", fetchone=True)
            orders = self.db.execute("SELECT COUNT(*) AS c FROM orders", fetchone=True)
            revenue = self.db.execute(
                "SELECT COALESCE(SUM(price),0) AS s FROM orders", fetchone=True
            )
            today = self.db.execute(
                """
                SELECT COUNT(*) AS c FROM orders
                WHERE created_at::date = CURRENT_DATE
                """,
                fetchone=True,
            )
            rev_today = self.db.execute(
                """
                SELECT COALESCE(SUM(price),0) AS s FROM orders
                WHERE created_at::date = CURRENT_DATE
                """,
                fetchone=True,
            )
            pending = self.db.execute(
                """
                SELECT COUNT(*) AS c FROM orders
                WHERE status IN ('pending','active','partial')
                """,
                fetchone=True,
            )
            user_bal = self.db.execute(
                "SELECT COALESCE(SUM(balance),0) AS s FROM users", fetchone=True
            )
            svc_count = self.db.execute(
                "SELECT COUNT(*) AS c FROM services WHERE active = TRUE", fetchone=True
            )
            balance = self.darkfollow.fetch_balance()
            stats = {
                "total_users": int((users or {}).get("c") or 0),
                "orders_today": int((today or {}).get("c") or 0),
                "revenue_today": float((rev_today or {}).get("s") or 0),
                "pending_orders": int((pending or {}).get("c") or 0),
                "total_services": int((svc_count or {}).get("c") or 0),
                "total_user_balance": float((user_bal or {}).get("s") or 0),
                "total_orders": int((orders or {}).get("c") or 0),
                "total_revenue": float((revenue or {}).get("s") or 0),
                "darkfollow_balance": balance.get("balance"),
            }
            return jsonify({"ok": True, "stats": stats, **stats, "darkfollow": balance})

        @sr("/admin/users", methods=["GET"])
        @admin
        def admin_users() -> Any:
            return jsonify({"ok": True, "users": self.users.list_users()})

        @sr("/admin/users/<int:user_id>/balance", methods=["POST"])
        @admin
        def admin_user_balance(user_id: int) -> Any:
            body = request.get_json(silent=True) or {}
            amount = float(body.get("amount") or 0)
            mode = str(body.get("mode") or "add")
            result = self.users.adjust_balance(user_id, amount, mode=mode)
            code = 200 if result.get("ok") else 400
            return jsonify(result), code

        @sr("/admin/darkfollow-balance", methods=["GET"])
        @admin
        def admin_darkfollow_balance() -> Any:
            data = self.darkfollow.fetch_balance()
            return jsonify(data)

        @sr("/admin/df-balance", methods=["GET"])
        @admin
        def admin_df_balance_alias() -> Any:
            return admin_darkfollow_balance()

        @sr("/admin/markup", methods=["GET", "POST"])
        @admin
        def admin_markup() -> Any:
            if request.method == "GET":
                return jsonify({"ok": True, "markup": self.markup.get_global_markup()})
            body = request.get_json(silent=True) or {}
            mtype = str(body.get("type") or body.get("markup_type") or "percent")
            mval = float(body.get("value") or body.get("markup_value") or 0)
            self.markup.set_global_markup(mtype, mval)
            updated = self.markup.recalculate_all_service_rates()
            return jsonify({"ok": True, "markup": self.markup.get_global_markup(), "rates_updated": updated})

        @sr("/admin/security-log", methods=["GET"])
        @admin
        def admin_security_log() -> Any:
            limit = int(request.args.get("limit", 100))
            rows = self.db.execute(
                """
                SELECT id, event_type, details, ip_address, created_at
                FROM security_log
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
                fetchall=True,
            ) or []
            events = []
            for r in rows:
                events.append(
                    {
                        "id": r["id"],
                        "event_type": r.get("event_type"),
                        "details": r.get("details"),
                        "ip_address": r.get("ip_address"),
                        "created_at": r.get("created_at").isoformat()
                        if r.get("created_at")
                        else None,
                    }
                )
            return jsonify({"ok": True, "events": events})

        @sr("/admin/cron-log", methods=["GET"])
        @admin
        def admin_cron_log() -> Any:
            limit = int(request.args.get("limit", 100))
            return jsonify({"ok": True, "logs": self.cron.list_logs(limit)})

        @sr("/admin/cron/run", methods=["POST"])
        @admin
        def admin_cron_run() -> Any:
            return jsonify(self.cron.run_maintenance())

        @sr("/admin/cron/simulate", methods=["POST"])
        @admin
        def admin_cron_simulate() -> Any:
            return jsonify(self.cron.simulate())

        @sr("/admin/refresh-catalog", methods=["POST"])
        @admin
        def admin_refresh_catalog() -> Any:
            self.catalog.deduplicate_database()
            threading.Thread(target=self.catalog.sync_from_provider, daemon=True).start()
            threading.Thread(target=self.darkfollow.fetch_balance, daemon=True).start()
            return jsonify(
                {
                    "ok": True,
                    "message": "Instant balance + catalog sync started in background",
                }
            )

        @sr("/admin/sync-catalog", methods=["POST"])
        @admin
        def admin_sync_catalog() -> Any:
            return jsonify(self.catalog.sync_from_provider())

        @sr("/admin/sync-prices", methods=["POST"])
        @admin
        def admin_sync_prices() -> Any:
            updated = self.catalog.refresh_rates()
            return jsonify({"ok": True, "updated": updated})

        @sr("/admin/services-status", methods=["GET"])
        @admin
        def admin_services_status() -> Any:
            return jsonify({"ok": True, **self.catalog.services_status()})

        @sr("/admin/categories", methods=["GET"])
        @admin
        def admin_categories() -> Any:
            return jsonify({"ok": True, "categories": self.catalog.list_categories_admin()})

        @sr("/admin/categories/<int:category_id>/markup", methods=["POST"])
        @admin
        def admin_category_markup(category_id: int) -> Any:
            body = request.get_json(silent=True) or {}
            mtype = str(body.get("type") or body.get("markup_type") or "percent")
            mval = float(body.get("value") or body.get("markup_value") or 0)
            ok = self.markup.set_category_markup(category_id, mtype, mval)
            if not ok:
                return jsonify({"ok": False, "error": "category not found"}), 404
            self.markup.recalculate_all_service_rates()
            return jsonify({"ok": True, "category_id": category_id, "markup_type": mtype, "markup_value": mval})

        @sr("/admin/categories/margin/bulk", methods=["POST"])
        @admin
        def admin_categories_margin_bulk() -> Any:
            body = request.get_json(silent=True) or {}
            if "categories" in body or "global_percent" in body:
                global_pct = float(body.get("global_percent") or 0)
                cats = body.get("categories") or []
                count = self.markup.apply_panel_margins(global_pct, cats)
            else:
                mtype = str(body.get("type") or body.get("markup_type") or "percent")
                mval = float(body.get("value") or body.get("markup_value") or 0)
                count = self.markup.bulk_category_margin(mtype, mval)
            updated = self.markup.recalculate_all_service_rates()
            return jsonify(
                {
                    "ok": True,
                    "categories_updated": count,
                    "rates_updated": updated,
                    "markup": self.markup.get_global_markup(),
                }
            )

        @sr("/user/balance", methods=["POST"])
        @admin
        def admin_user_balance_by_uid() -> Any:
            body = request.get_json(silent=True) or {}
            uid = str(body.get("uid") or body.get("WF_uid") or "")
            amount = float(body.get("amount") or 0)
            result = self.users.topup_by_uid(uid, amount)
            code = 200 if result.get("ok") else 400
            return jsonify(result), code

        @sr("/user/find", methods=["GET"])
        @admin
        def admin_user_find() -> Any:
            query = request.args.get("q") or request.args.get("uid") or ""
            row = self.users.find_user(query)
            if not row:
                return jsonify({"ok": False, "error": "not found"}), 404
            return jsonify({"ok": True, "user": self.users.serialize_user(row)})

        # ── User auth ───────────────────────────────────────────────────────

        @sr("/auth/register", methods=["POST"])
        def auth_register() -> Any:
            body = request.get_json(silent=True) or {}
            result = self.users.register(
                str(body.get("email") or ""),
                str(body.get("username") or ""),
                str(body.get("password") or ""),
            )
            code = 200 if result.get("ok") else 400
            return jsonify(result), code

        @sr("/auth/login", methods=["POST"])
        def auth_login() -> Any:
            body = request.get_json(silent=True) or {}
            identifier = str(body.get("email") or body.get("identifier") or "")
            password = str(body.get("password") or "")
            result = self.users.login(identifier, password)
            code = 200 if result.get("ok") else 401
            return jsonify(result), code

        @sr("/auth/logout", methods=["POST"])
        def auth_logout() -> Any:
            token = request.headers.get("X-User-Token", "").strip()
            self.users.logout(token)
            return jsonify({"ok": True})

        @sr("/auth/reset-request", methods=["POST"])
        def auth_reset_request() -> Any:
            body = request.get_json(silent=True) or {}
            email = str(body.get("email") or "").strip().lower()
            if not email:
                return jsonify({"ok": False, "error": "email required"}), 400
            return jsonify(
                {
                    "ok": True,
                    "message": "If the email exists, reset instructions were sent.",
                }
            )

        # ── Storefront ────────────────────────────────────────────────────

        @sr("/user/me", methods=["GET"])
        @user_auth
        def user_me(user: Dict[str, Any]) -> Any:
            return jsonify(self.users.serialize_user(user))

        @sr("/services/list", methods=["GET"])
        def services_list() -> Any:
            lang = request.args.get("lang", "ar")
            categories = self.catalog.list_for_storefront(lang)
            return jsonify({"ok": True, "categories": categories})

        @sr("/order/place", methods=["POST"])
        @user_auth
        def order_place(user: Dict[str, Any]) -> Any:
            body = request.get_json(silent=True) or {}
            service_id = int(body.get("service_id") or 0)
            link = str(body.get("link") or "")
            quantity = int(body.get("quantity") or 0)
            result = self.users.place_order(user, service_id, link, quantity)
            code = 200 if result.get("ok") else 400
            return jsonify(result), code

        @sr("/user/orders", methods=["GET"])
        @user_auth
        def user_orders(user: Dict[str, Any]) -> Any:
            return jsonify({"ok": True, "orders": self.users.list_orders(int(user["id"]))})

        @sr("/payment/gateways", methods=["GET"])
        def payment_gateways() -> Any:
            rows = self.db.execute(
                """
                SELECT id, name, active, sort_order
                FROM payment_gateways
                WHERE active = TRUE
                ORDER BY sort_order, id
                """,
                fetchall=True,
            ) or []
            return jsonify(
                {
                    "ok": True,
                    "gateways": [
                        {"id": r["id"], "name": r["name"], "active": r["active"]}
                        for r in rows
                    ],
                }
            )

        @sr("/payment/submit", methods=["POST"])
        @user_auth
        def payment_submit(user: Dict[str, Any]) -> Any:
            body = request.get_json(silent=True) or {}
            amount = float(body.get("amount") or 0)
            if amount <= 0:
                return jsonify({"ok": False, "error": "invalid amount"}), 400
            note = str(body.get("note") or "")
            method = str(body.get("method") or "manual")
            row = self.db.execute(
                """
                INSERT INTO payments (user_id, amount, method, note, status)
                VALUES (%s, %s, %s, %s, 'pending')
                RETURNING id
                """,
                (user["id"], amount, method, note),
                fetchone=True,
            )
            return jsonify({"ok": True, "payment_id": row["id"]})

        @sr("/tickets", methods=["GET"])
        @user_auth
        def tickets_list(user: Dict[str, Any]) -> Any:
            rows = self.db.execute(
                """
                SELECT id, subject, status, created_at
                FROM tickets WHERE user_id = %s
                ORDER BY id DESC
                """,
                (user["id"],),
                fetchall=True,
            ) or []
            return jsonify(
                {
                    "ok": True,
                    "tickets": [
                        {
                            "id": r["id"],
                            "subject": r.get("subject"),
                            "status": r.get("status"),
                            "created_at": r.get("created_at").isoformat()
                            if r.get("created_at")
                            else None,
                        }
                        for r in rows
                    ],
                }
            )

        @sr("/tickets/create", methods=["POST"])
        @user_auth
        def tickets_create(user: Dict[str, Any]) -> Any:
            body = request.get_json(silent=True) or {}
            subject = str(body.get("subject") or "").strip()
            message = str(body.get("message") or "").strip()
            if not subject:
                return jsonify({"ok": False, "error": "subject required"}), 400
            row = self.db.execute(
                """
                INSERT INTO tickets (user_id, subject, message, status)
                VALUES (%s, %s, %s, 'open')
                RETURNING id
                """,
                (user["id"], subject, message),
                fetchone=True,
            )
            return jsonify({"ok": True, "ticket_id": row["id"]})

        @sr("/settings/public", methods=["GET"])
        def settings_public() -> Any:
            return jsonify(
                {
                    "ok": True,
                    "faq_ar": self.markup.get_setting("faq_ar", "[]"),
                    "tos_ar": self.markup.get_setting("tos_ar", ""),
                    "terms": self.markup.get_setting("tos_ar", ""),
                }
            )


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

panel = PanelApplication(app, AppConfig())
panel.initialize()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
