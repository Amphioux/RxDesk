"""
RxDesk Backend - database.py
===============================
Everything related to talking to the live MASSPRO ERP database lives
HERE, and only here. main.py and every router (pharmacy.py, mr.py,
admin routes) import run_query() from this file instead of opening
their own pyodbc connections. That way the READ UNCOMMITTED safety
rule and the fiscal-year database discovery logic can never
accidentally be skipped or duplicated somewhere else.
"""

import os
from datetime import date, datetime
from typing import Optional

import pyodbc

# ------------------------------------------------------------------
# 1. SQL SERVER (ERP) CONNECTION CONFIG
# ------------------------------------------------------------------
SQL_SERVER_HOST = os.getenv("MASSPRO_SQL_HOST", "localhost")
SQL_SERVER_PORT = os.getenv("MASSPRO_SQL_PORT", "1433")
SQL_SERVER_USER = os.getenv("MASSPRO_SQL_USER", "sa")
SQL_SERVER_PASSWORD = os.getenv("MASSPRO_SQL_PASSWORD", "")
SQL_SERVER_DRIVER = os.getenv("MASSPRO_SQL_DRIVER", "{ODBC Driver 17 for SQL Server}")

# MASSPRO stores each Nepali fiscal year (Bikram Sambat) in its own
# database, named "ST" + a 4-digit BS year code, e.g. ST8384 for FY
# 2083-84 BS. See discover_active_year_db() below.
MASSPRO_DB_PREFIX = os.getenv("MASSPRO_DB_PREFIX", "ST")

# Cached so we don't re-run the discovery query on every single API call.
_active_db_cache: Optional[str] = None


def get_master_connection() -> pyodbc.Connection:
    """Connects to SQL Server's built-in 'master' database only - used
    purely to look up which yearly MASSPRO (ST####) databases exist."""
    conn_str = (
        f"DRIVER={SQL_SERVER_DRIVER};"
        f"SERVER={SQL_SERVER_HOST},{SQL_SERVER_PORT};"
        f"DATABASE=master;"
        f"UID={SQL_SERVER_USER};"
        f"PWD={SQL_SERVER_PASSWORD};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=5)


def discover_active_year_db(force_refresh: bool = False) -> str:
    """
    Queries sys.databases for names matching 'ST' + exactly 4 digits
    (e.g. ST8384) and returns the one with the highest 4-digit BS year
    code, i.e. the current Nepali fiscal year's database. Cached
    in-memory after the first successful call.

    NOTE: this simple "highest number wins" comparison will need a
    small tweak whenever the BS calendar rolls from ...99 back to ...00
    (i.e. BS 2099 -> 2100), roughly 15 years from now.
    """
    global _active_db_cache
    if _active_db_cache is not None and not force_refresh:
        return _active_db_cache

    conn = get_master_connection()
    cursor = conn.cursor()
    like_pattern = f"{MASSPRO_DB_PREFIX}[0-9][0-9][0-9][0-9]"
    cursor.execute("SELECT name FROM sys.databases WHERE name LIKE ?", (like_pattern,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        raise RuntimeError(
            f"No ERP fiscal-year databases found matching pattern "
            f"'{MASSPRO_DB_PREFIX}' + 4 digits (e.g. {MASSPRO_DB_PREFIX}8384). "
            f"Check the MASSPRO_DB_PREFIX environment variable and confirm "
            f"the SQL login has permission to list databases."
        )

    def extract_year_suffix(db_name: str) -> int:
        suffix = db_name[len(MASSPRO_DB_PREFIX):]
        return int(suffix) if suffix.isdigit() and len(suffix) == 4 else -1

    db_names = [row[0] for row in rows]
    db_names.sort(key=extract_year_suffix, reverse=True)

    _active_db_cache = db_names[0]
    return _active_db_cache


def get_erp_connection() -> pyodbc.Connection:
    """Opens a connection to whichever MASSPRO yearly database is currently active."""
    active_db = discover_active_year_db()
    conn_str = (
        f"DRIVER={SQL_SERVER_DRIVER};"
        f"SERVER={SQL_SERVER_HOST},{SQL_SERVER_PORT};"
        f"DATABASE={active_db};"
        f"UID={SQL_SERVER_USER};"
        f"PWD={SQL_SERVER_PASSWORD};"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=10)


# ------------------------------------------------------------------
# 2. SAFE QUERY RUNNER
# ------------------------------------------------------------------
def run_query(sql: str, params: tuple = ()) -> list[dict]:
    """
    Runs a SELECT query against the live ERP database.

    THIS IS THE ONLY FUNCTION THAT SHOULD EVER TALK TO THE ERP DATABASE.
    Every router calls this instead of opening its own pyodbc
    connection, so the safety rule below is never accidentally skipped.

    ALWAYS sets READ UNCOMMITTED isolation first, per RxDesk's Database
    Safety Rule - this guarantees our reporting queries place ZERO locks
    on tables the live ERP is actively writing sales into.

    Returns a list of dicts, e.g. [{"Bill_No": 123, "Amount": 4500.0}, ...]
    """
    conn = get_erp_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED;")
        cursor.execute(sql, params)
        columns = [column[0] for column in cursor.description]
        results = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return results
    finally:
        cursor.close()
        conn.close()


# ------------------------------------------------------------------
# 3. SMALL HELPERS SHARED BY ROUTERS
# ------------------------------------------------------------------
def to_iso(value) -> Optional[str]:
    """
    Converts a pyodbc date/datetime value into an ISO-formatted string
    safe for JSON responses (e.g. '2083-04-15' or '2083-04-15T00:00:00').
    Passes None straight through.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def to_date(value) -> Optional[date]:
    """
    Normalizes a pyodbc date/datetime value down to a plain
    datetime.date, used for day-count math (e.g. near-expiry windows).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None
