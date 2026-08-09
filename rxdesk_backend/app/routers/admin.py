"""
RxDesk Backend - routers/admin.py
=====================================
Endpoints exclusively for the ADMIN (Distributor Admin) role.

Covers:
  - User management: create RxDesk logins, list existing ones.
  - ERP health check: confirm SQL Server connectivity and show the
    currently active fiscal-year (ST####) database.
  - Dashboard: total receivables, total returns, and stock-expiry
    financial risk across the WHOLE distributorship.

Note the difference from mr.py: nothing in this file is scoped to a
single Company_Code or GlCode. An Admin is meant to see every
customer and every brand, which is exactly why every endpoint here is
locked to the ADMIN role only, via the router-level dependency below.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import auth, database
from ..database import run_query
from ..models import AdminDashboardSummary, CompanyExpiryRisk

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"],
    dependencies=[Depends(auth.require_role(["ADMIN"]))],
)


# ------------------------------------------------------------------
# USER MANAGEMENT
# ------------------------------------------------------------------
@router.post("/create-user", response_model=auth.UserOut)
async def create_user_endpoint(new_user: auth.UserCreate):
    """
    Creates a new RxDesk login and maps it to an ERP identifier:
      - role="PHARMACY" -> erp_code = General_Ledger.GlCode
      - role="MR"        -> erp_code = Product_Company.Company_Code
      - role="ADMIN"     -> erp_code not required
    """
    return auth.create_user(new_user)


@router.get("/users", response_model=list[auth.UserOut])
async def list_users_endpoint():
    """Lists every RxDesk login, for the user-management screen."""
    return auth.list_all_users()


# ------------------------------------------------------------------
# ERP HEALTH CHECK
# ------------------------------------------------------------------
@router.get("/erp-status")
async def erp_status():
    """Confirms RxDesk can reach SQL Server and shows the active fiscal-year database."""
    try:
        active_db = database.discover_active_year_db(force_refresh=True)
        return {"status": "connected", "active_database": active_db}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Could not reach ERP SQL Server: {str(e)}",
        )


# ------------------------------------------------------------------
# DASHBOARD
# ------------------------------------------------------------------
@router.get("/dashboard", response_model=AdminDashboardSummary)
async def get_dashboard(
    expiry_risk_days: int = Query(
        90,
        ge=1,
        le=365,
        description="Batches expiring within this many days count toward 'stock expiry financial risk'",
    ),
):
    """
    High-level distributorship overview for the admin's home screen:
      - total_receivables: sum of every POSITIVE General_Ledger balance
        (every customer who currently owes money), across ALL brands -
        pulled straight from the ERP's own ledger balance, since that's
        the authoritative figure (rather than recalculating from bills).
      - total_returns_amount: sum of every Sale_Return + Sale_Exp_Return
        row in the active fiscal year.
      - stock_expiry_risk_value: total sell-value (QTY x Sell Rate) of
        batches expiring within expiry_risk_days, broken down by
        company/brand so the admin can see which brands carry the most
        expiry exposure.
    """
    # --- Total receivables (money owed TO the distributor) ---
    receivable_rows = run_query(
        """
        SELECT SUM([Current Balance]) AS TotalReceivables, COUNT(*) AS CustomerCount
        FROM General_Ledger
        WHERE [Current Balance] > 0
        """
    )
    total_receivables = float(receivable_rows[0]["TotalReceivables"] or 0) if receivable_rows else 0.0
    customers_with_dues = int(receivable_rows[0]["CustomerCount"] or 0) if receivable_rows else 0

    # --- Total returns (regular sales returns + expired-stock credit notes) ---
    sale_return_rows = run_query("SELECT SUM(Amount) AS TotalAmount, COUNT(*) AS Cnt FROM Sale_Return")
    exp_return_rows = run_query("SELECT SUM(Amount) AS TotalAmount, COUNT(*) AS Cnt FROM Sale_Exp_Return")

    sale_return_total = float(sale_return_rows[0]["TotalAmount"] or 0) if sale_return_rows else 0.0
    sale_return_count = int(sale_return_rows[0]["Cnt"] or 0) if sale_return_rows else 0
    exp_return_total = float(exp_return_rows[0]["TotalAmount"] or 0) if exp_return_rows else 0.0
    exp_return_count = int(exp_return_rows[0]["Cnt"] or 0) if exp_return_rows else 0

    total_returns_amount = round(sale_return_total + exp_return_total, 2)
    total_return_count = sale_return_count + exp_return_count

    # --- Stock expiry financial risk, broken down by company/brand ---
    cutoff_date = date.today() + timedelta(days=expiry_risk_days)
    expiry_rows = run_query(
        """
        SELECT pc.Company_Code, pc.Name AS CompanyName,
               SUM(b.QTY * b.[Sell Rate]) AS AtRiskValue,
               COUNT(*) AS BatchCount
        FROM [Batch Details] b
        INNER JOIN ProductMaster p ON p.itemCode = b.ItemCode
        INNER JOIN Product_Company pc ON pc.Company_Code = p.Company_Code
        WHERE b.Expiry <= ?
          AND b.QTY > 0
          AND (b.blocked IS NULL OR b.blocked = 0)
        GROUP BY pc.Company_Code, pc.Name
        ORDER BY SUM(b.QTY * b.[Sell Rate]) DESC
        """,
        (cutoff_date,),
    )

    expiry_by_company = [
        CompanyExpiryRisk(
            Company_Code=r["Company_Code"],
            CompanyName=r["CompanyName"],
            at_risk_value=round(float(r["AtRiskValue"] or 0), 2),
            batch_count=int(r["BatchCount"] or 0),
        )
        for r in expiry_rows
    ]
    total_expiry_risk = round(sum(c.at_risk_value for c in expiry_by_company), 2)

    return AdminDashboardSummary(
        total_receivables=round(total_receivables, 2),
        total_customers_with_dues=customers_with_dues,
        total_returns_amount=total_returns_amount,
        total_return_count=total_return_count,
        expiry_risk_window_days=expiry_risk_days,
        stock_expiry_risk_value=total_expiry_risk,
        expiry_risk_by_company=expiry_by_company,
    )
