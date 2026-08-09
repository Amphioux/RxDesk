"""
RxDesk Backend - main.py
===========================
The FastAPI application entrypoint. This file wires everything
together: CORS, startup tasks, and every router.

    app/database.py           -> SQL Server connection + safe query runner
    app/auth.py                 -> SQLite logins, bcrypt, JWT
    app/models.py                 -> ERP-data response schemas
    app/routers/pharmacy.py       -> ledger, invoices, returns (PHARMACY role)
    app/routers/mr.py             -> stock, near-expiry, sales, receivables (MR role)
    app/routers/admin.py          -> user management, ERP status, dashboard (ADMIN role)

main.py itself only keeps the two "who am I" auth endpoints
(/api/auth/login, /api/auth/me), since those run before any role is
even known yet - everything role-specific lives in its own router.
"""

from datetime import timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm

from . import auth
from .routers import admin, mr, pharmacy

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


# Wire in every role-specific router. Each one already enforces its own
# role restriction internally via `dependencies=[Depends(auth.require_role([...]))]`.
app.include_router(pharmacy.router)
app.include_router(mr.router)
app.include_router(admin.router)


# ------------------------------------------------------------------
# 2. AUTH ENDPOINTS (apply to every role, before a role is even known)
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
# 3. ROOT / HEALTH CHECK
# ------------------------------------------------------------------
@app.get("/", tags=["Health"])
async def root():
    """Unauthenticated health check - useful for confirming the server is up."""
    return {"app": "RxDesk API", "status": "running"}
