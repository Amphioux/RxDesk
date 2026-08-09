"""
RxDesk Backend - auth.py
=========================
Handles everything related to WHO is logged in and WHAT they're allowed to do.

Responsibilities:
  1. A local SQLite database (app_users.db) that stores RxDesk app logins.
     This is completely separate from the SQL Server MASSPRO ERP database.
     Pharmacies, MRs, and Admins log into RxDesk with these credentials,
     and each login is MAPPED to an ERP identifier (GlCode or Company_Code).
  2. Password hashing using bcrypt (via passlib) - we never store plain text.
  3. JWT (JSON Web Token) creation and verification (via python-jose).
  4. FastAPI dependencies: get_current_user() and require_role() which
     other route files will import to protect endpoints.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field

# ----------------------------------------------------------------------
# 1. CONFIGURATION
# ----------------------------------------------------------------------
# In production, set RXDESK_SECRET_KEY as a real environment variable
# (e.g. in your .env file). Never commit a real secret key to git.
# Generate a strong one with: python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY = os.getenv(
    "RXDESK_SECRET_KEY",
    "CHANGE_THIS_TO_A_LONG_RANDOM_STRING_IN_PRODUCTION",
)
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 12  # tokens are valid for 12 hours

# Path to the local SQLite DB that stores app logins.
# This resolves to rxdesk_backend/app_users.db regardless of where
# uvicorn is launched from.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_DB_PATH = os.path.join(BASE_DIR, "app_users.db")

# The only roles RxDesk understands. Anything else is rejected.
VALID_ROLES = ["PHARMACY", "MR", "ADMIN"]

# ----------------------------------------------------------------------
# 2. PASSWORD HASHING SETUP
# ----------------------------------------------------------------------
# CryptContext handles bcrypt hashing + verification for us safely.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_password_hash(plain_password: str) -> str:
    """Turns a plain-text password into a secure bcrypt hash for storage."""
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Checks a plain-text password against a stored bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ----------------------------------------------------------------------
# 3. OAUTH2 SCHEME
# ----------------------------------------------------------------------
# This tells FastAPI's auto-generated docs (/docs) where the login
# endpoint lives, so the "Authorize" button works out of the box.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


# ----------------------------------------------------------------------
# 4. PYDANTIC MODELS (data shapes used across the app)
# ----------------------------------------------------------------------
class UserCreate(BaseModel):
    """Shape of data the Admin sends when creating a new login."""
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6)
    full_name: str
    role: str  # "PHARMACY", "MR", or "ADMIN"
    # For PHARMACY -> this is the General_Ledger.GlCode
    # For MR       -> this is the Product_Company.Company_Code (their brand)
    # For ADMIN    -> leave this blank/null
    erp_code: Optional[str] = None


class UserOut(BaseModel):
    """Shape of data we SEND BACK about a user (never includes the password)."""
    id: int
    username: str
    full_name: str
    role: str
    erp_code: Optional[str]
    is_active: bool
    created_at: str


class UserInDB(UserOut):
    """Internal-only shape that includes the hashed password. Never returned via API."""
    hashed_password: str


class Token(BaseModel):
    """Shape of the response returned right after a successful login."""
    access_token: str
    token_type: str
    role: str
    full_name: str
    erp_code: Optional[str]


class TokenData(BaseModel):
    """Shape of the data we decode out of a JWT."""
    username: Optional[str] = None
    role: Optional[str] = None
    erp_code: Optional[str] = None


# ----------------------------------------------------------------------
# 5. SQLITE DATABASE INITIALIZATION
# ----------------------------------------------------------------------
def get_sqlite_connection() -> sqlite3.Connection:
    """Opens a fresh connection to app_users.db with dict-like row access."""
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db() -> None:
    """
    Creates the 'users' table if it doesn't exist yet, and seeds a
    default ADMIN account on first run so you're never locked out.
    Called once on FastAPI startup (see main.py).
    """
    conn = get_sqlite_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            erp_code TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # Check if any admin already exists
    cursor.execute("SELECT COUNT(*) as cnt FROM users WHERE role = 'ADMIN'")
    admin_count = cursor.fetchone()["cnt"]

    if admin_count == 0:
        default_admin_password = os.getenv("RXDESK_DEFAULT_ADMIN_PASSWORD", "Admin@123")
        cursor.execute(
            """
            INSERT INTO users
                (username, hashed_password, full_name, role, erp_code, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "admin",
                get_password_hash(default_admin_password),
                "RxDesk Administrator",
                "ADMIN",
                None,
                1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        print("=" * 64)
        print("First run detected -> default ADMIN account created:")
        print(f"   username: admin")
        print(f"   password: {default_admin_password}")
        print("   >>> LOG IN AND CHANGE THIS PASSWORD IMMEDIATELY <<<")
        print("=" * 64)

    conn.close()


# ----------------------------------------------------------------------
# 6. USER CRUD HELPERS
# ----------------------------------------------------------------------
def get_user_by_username(username: str) -> Optional[UserInDB]:
    """Fetches one user row by username, or None if it doesn't exist."""
    conn = get_sqlite_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()

    if row is None:
        return None

    return UserInDB(
        id=row["id"],
        username=row["username"],
        hashed_password=row["hashed_password"],
        full_name=row["full_name"],
        role=row["role"],
        erp_code=row["erp_code"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def create_user(user: UserCreate) -> UserOut:
    """
    Inserts a new login into app_users.db.
    Raises HTTPException(400) if the role is invalid or the username
    is already taken.
    """
    if user.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{user.role}'. Must be one of {VALID_ROLES}",
        )

    if user.role in ("PHARMACY", "MR") and not user.erp_code:
        raise HTTPException(
            status_code=400,
            detail=f"erp_code is required for role '{user.role}' "
                   f"(GlCode for PHARMACY, Company_Code for MR)",
        )

    existing = get_user_by_username(user.username)
    if existing is not None:
        raise HTTPException(status_code=400, detail="Username already exists")

    conn = get_sqlite_connection()
    cursor = conn.cursor()
    created_at = datetime.now(timezone.utc).isoformat()
    cursor.execute(
        """
        INSERT INTO users
            (username, hashed_password, full_name, role, erp_code, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.username,
            get_password_hash(user.password),
            user.full_name,
            user.role,
            user.erp_code,
            1,
            created_at,
        ),
    )
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    return UserOut(
        id=new_id,
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        erp_code=user.erp_code,
        is_active=True,
        created_at=created_at,
    )


def list_all_users() -> List[UserOut]:
    """Returns every RxDesk login, for the Admin's user-management screen."""
    conn = get_sqlite_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users ORDER BY id ASC")
    rows = cursor.fetchall()
    conn.close()

    return [
        UserOut(
            id=r["id"],
            username=r["username"],
            full_name=r["full_name"],
            role=r["role"],
            erp_code=r["erp_code"],
            is_active=bool(r["is_active"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def authenticate_user(username: str, password: str) -> Optional[UserInDB]:
    """
    Checks username + password.
    Returns the UserInDB record if valid and active, otherwise None.
    """
    user = get_user_by_username(username)
    if user is None:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    if not user.is_active:
        return None
    return user


# ----------------------------------------------------------------------
# 7. JWT TOKEN CREATION
# ----------------------------------------------------------------------
def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Encodes a dict (e.g. {"sub": username, "role": role, "erp_code": code})
    into a signed JWT string that expires after expires_delta.
    """
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


# ----------------------------------------------------------------------
# 8. FASTAPI DEPENDENCIES (used to protect routes)
# ----------------------------------------------------------------------
credentials_exception = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(token: str = Depends(oauth2_scheme)) -> UserInDB:
    """
    Decodes the JWT sent in the 'Authorization: Bearer <token>' header,
    and loads the matching user from SQLite.
    Any route that adds `Depends(get_current_user)` is now login-protected.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = get_user_by_username(username)
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated")
    return user


def require_role(allowed_roles: List[str]):
    """
    Factory for a role-checking dependency.
    Usage in a route: Depends(require_role(["ADMIN"]))
    Usage for multiple roles: Depends(require_role(["ADMIN", "MR"]))
    """

    async def role_checker(current_user: UserInDB = Depends(get_current_user)) -> UserInDB:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Role '{current_user.role}' is not permitted to access this "
                    f"resource. Requires one of: {allowed_roles}"
                ),
            )
        return current_user

    return role_checker
