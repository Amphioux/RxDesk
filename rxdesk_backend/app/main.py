"""
RxDesk Backend - main.py (Part 1)
===================================
Today this file contains:
  1. FastAPI app instance + CORS setup
  2. SQL Server (pyodbc) connection handling, including automatic
     discovery of MASSPRO's "active financial year" database.
  3. A safe query runner that ALWAYS applies
     SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED
     before touching the live ERP data (RxDesk's core safety rule).
  4. Auth endpoints: /api/auth/login, /api/auth/me
  5. Admin endpoints: /api/admin/create-user, /api/admin/users, /api/admin/erp-status

Later phases will add app/routers/pharmacy.py, app/routers/mr.py, and
app/routers/admin.py for the actual business endpoints (ledger, invoices,
stock, near-expiry, etc.) and wire them in here with app.include_router().
"""

import os
from datetime import timedelta
from typing import Optional

import pyodbc
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from . import auth

# Load variables from a .env file (if present) into os.environ
load_dotenv()

# ------------------------------------------------------------------
# 1. APP INITIALIZATION
# ------------------------------------------------------------------
app = FastAPI(
    title="RxDesk API",
    description="Backend API for RxDesk, connecting to the MASSPRO ERP.",
    version="1.0.0",
)

# CORS: allows your Flutter app (running on a phone/emulator, different
# origin) to call this API. Tighten allow_origins to your real domain
# once you go to production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    """Runs once when the server boots: makes sure the SQLite auth DB exists."""
    auth.init_auth_db()
    print("RxDesk API started. SQLite auth DB is ready.")


# ------------------------------------------------------------------
# 2. SQL SERVER (ERP) CONNECTION CONFIG
# ------------------------------------------------------------------
# All of these should be set in your .env file, e.g.:
#   MASSPRO_SQL_HOST=192.168.1.50
#   MASSPRO_SQL_PORT=1433
#   MASSPRO_SQL_USER=rxdesk_reader
#   MASSPRO_SQL_PASSWORD=your_password_here
#   MASSPRO_SQL_DRIVER={ODBC Driver 17 for SQL Server}
#   MASSPRO_DB_PREFIX=MASSPRO_ST
SQL_SERVER_HOST = os.getenv("MASSPRO_SQL_HOST", "localhost")
SQL_SERVER_PORT = os.getenv("MASSPRO_SQL_PORT", "1433")
SQL_SERVER_USER = os.getenv("MASSPRO_SQL_USER", "sa")
SQL_SERVER_PASSWORD = os.getenv("MASSPRO_SQL_PASSWORD", "")
SQL_SERVER_DRIVER = os.getenv("MASSPRO_SQL_DRIVER", "{ODBC Driver 17 for SQL Server}")

# CONFIRMED: MASSPRO stores each Nepali fiscal year's transactional data
# in its own database, named "ST" + a 4-digit Bikram Sambat (BS) year
# code, e.g.:
#   ST8081  -> fiscal year 2080-81 BS
#   ST8182  -> fiscal year 2081-82 BS
#   ST8283  -> fiscal year 2082-83 BS
#   ST8384  -> fiscal year 2083-84 BS  (current, as of writing)
# RxDesk asks SQL Server which ST######## databases exist and picks the
# one with the highest 4-digit number, so it keeps working automatically
# each Shrawan (Nepali new fiscal year) without any code change or
# redeploy - your ERP admin just creates the new ST#### database as usual.
#
# NOTE: this simple "highest number wins" comparison will need a small
# tweak whenever the BS calendar rolls from ...99 back to ...00 (i.e.
# BS 2099 -> 2100), since e.g. 8099 > 8100 numerically even though 8100
# is the later year. That rollover is roughly 15 years away, so it is
# not handled yet - flag it to me closer to that date and I'll patch
# extract_year_suffix() to handle the wraparound.
MASSPRO_DB_PREFIX = os.getenv("MASSPRO_DB_PREFIX", "ST")

# Cached so we don't re-run the discovery query on every single API call.
_active_db_cache: Optional[str] = None


def get_master_connection() -> pyodbc.Connection:
    """Connects to SQL Server's built-in 'master' database only - used
    purely to look up which yearly MASSPRO databases exist."""
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
    """
    global _active_db_cache
    if _active_db_cache is not None and not force_refresh:
        return _active_db_cache

    conn = get_master_connection()
    cursor = conn.cursor()
    # SQL Server's LIKE supports character-range wildcards ([0-9]), which
    # lets us match "ST" followed by EXACTLY 4 digits (e.g. ST8384) and
    # nothing else. This deliberately excludes unrelated databases that
    # might merely start with "ST" (e.g. a "STAGING" database) as well
    # as malformed names, instead of a loose "ST%" match.
    like_pattern = f"{MASSPRO_DB_PREFIX}[0-9][0-9][0-9][0-9]"
    cursor.execute("SELECT name FROM sys.databases WHERE name LIKE ?", (like_pattern,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        raise RuntimeError(
            f"No ERP fiscal-year databases found matching pattern "
            f"'{MASSPRO_DB_PREFIX}' + 4 digits (e.g. {MASSPRO_DB_PREFIX}8384). "
            f"Check the MASSPRO_DB_PREFIX environment variable and confirm "
            f"the SQL login has permission to list databases (VIEW ANY "
            f"DEFINITION or sysadmin on the server)."
        )

    def extract_year_suffix(db_name: str) -> int:
        """
        Extracts the 4-digit BS year code as an integer, e.g. "ST8384" -> 8384.
        Higher number = later fiscal year (see the wraparound note above
        the MASSPRO_DB_PREFIX definition for the one long-term edge case).
        """
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
# 3. SAFE QUERY RUNNER
# ------------------------------------------------------------------
def run_query(sql: str, params: tuple = ()) -> list[dict]:
    """
    Runs a SELECT query against the live ERP database.

    THIS IS THE ONLY FUNCTION THAT SHOULD EVER TALK TO THE ERP DATABASE.
    Every future router (pharmacy.py, mr.py, admin.py) should call this
    instead of opening its own pyodbc connection, so the safety rule
    below is never accidentally skipped.

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
# 4. AUTH ENDPOINTS
# ------------------------------------------------------------------
@app.post("/api/auth/login", response_model=auth.Token, tags=["Auth"])
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Logs a user in. Expects standard OAuth2 form fields: 'username' and
    'password' (sent as x-www-form-urlencoded, which OAuth2PasswordRequestForm
    handles for you). Returns a JWT access token plus role/erp_code so the
    Flutter app immediately knows which screens to show.
    """
    user = auth.authenticate_user(form_data.username, form_data.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = auth.create_access_token(
        data={"sub": user.username, "role": user.role, "erp_code": user.erp_code},
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )

    return auth.Token(
        access_token=access_token,
        token_type="bearer",
        role=user.role,
        full_name=user.full_name,
        erp_code=user.erp_code,
    )


@app.get("/api/auth/me", response_model=auth.UserOut, tags=["Auth"])
async def read_current_user(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """Returns the profile of whoever's token is currently being used. Handy for the app's 'Profile' screen and for verifying a token is still valid."""
    return auth.UserOut(
        id=current_user.id,
        username=current_user.username,
        full_name=current_user.full_name,
        role=current_user.role,
        erp_code=current_user.erp_code,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
    )


# ------------------------------------------------------------------
# 5. ADMIN ENDPOINTS
# ------------------------------------------------------------------
@app.post("/api/admin/create-user", response_model=auth.UserOut, tags=["Admin"])
async def create_user_endpoint(
    new_user: auth.UserCreate,
    current_admin: auth.UserInDB = Depends(auth.require_role(["ADMIN"])),
):
    """
    ADMIN ONLY. Creates a new RxDesk login and maps it to an ERP identifier:
      - role="PHARMACY" -> erp_code = General_Ledger.GlCode
      - role="MR"        -> erp_code = Product_Company.Company_Code
      - role="ADMIN"     -> erp_code not required
    """
    return auth.create_user(new_user)


@app.get("/api/admin/users", response_model=list[auth.UserOut], tags=["Admin"])
async def list_users_endpoint(
    current_admin: auth.UserInDB = Depends(auth.require_role(["ADMIN"])),
):
    """ADMIN ONLY. Lists every RxDesk login for the user-management screen."""
    return auth.list_all_users()


@app.get("/api/admin/erp-status", tags=["Admin"])
async def erp_status(current_admin: auth.UserInDB = Depends(auth.require_role(["ADMIN"]))):
    """
    ADMIN ONLY. Quick health check confirming RxDesk can reach SQL Server
    and showing which yearly ERP database it's currently talking to.
    """
    try:
        active_db = discover_active_year_db(force_refresh=True)
        return {"status": "connected", "active_database": active_db}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach ERP SQL Server: {str(e)}",
        )


# ------------------------------------------------------------------
# 6. ROOT / HEALTH CHECK
# ------------------------------------------------------------------
@app.get("/", tags=["Health"])
async def root():
    """Unauthenticated health check - useful for confirming the server is up."""
    return {"app": "RxDesk API", "status": "running"}
