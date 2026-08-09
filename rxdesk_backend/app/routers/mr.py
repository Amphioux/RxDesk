"""
RxDesk Backend - routers/mr.py
=================================
Endpoints exclusively for the MR (Medical Representative) role.

SECURITY RULE: every query is scoped to current_user.erp_code, which
for an MR holds their assigned Product_Company.Company_Code (their
"brand"). This comes from the verified JWT, never from a client
parameter, so an MR can only ever see stock, near-expiry batches,
sales, and receivables tied to THEIR OWN brand - never a competitor's
or another rep's brand data.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from .. import auth
from ..database import run_query, to_date, to_iso
from ..models import (
    MRInvoiceDetail,
    MRInvoiceLineItem,
    MRInvoiceSummary,
    NearExpiryBatch,
    ReceivableByCustomer,
    StockBatch,
)

router = APIRouter(
    prefix="/api/mr",
    tags=["Medical Representative"],
    dependencies=[Depends(auth.require_role(["MR"]))],
)


# ------------------------------------------------------------------
# BRAND STOCK
# ------------------------------------------------------------------
@router.get("/stock", response_model=list[StockBatch])
async def get_brand_stock(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """
    Returns every batch, across every item, that belongs to this MR's
    assigned Company_Code. Blocked batches are included but flagged via
    the 'blocked' field so the Flutter app can grey them out rather
    than hide them entirely.
    """
    company_code = current_user.erp_code

    rows = run_query(
        """
        SELECT p.itemCode, p.Name AS ItemName, p.Pack,
               b.[Batch No] AS BatchNo, b.Expiry, b.QTY,
               b.[Sell Rate] AS SellRate, b.MRP, b.blocked
        FROM ProductMaster p
        INNER JOIN [Batch Details] b ON b.ItemCode = p.itemCode
        WHERE p.Company_Code = ?
        ORDER BY p.Name ASC, b.Expiry ASC
        """,
        (company_code,),
    )

    return [
        StockBatch(
            ItemCode=r["itemCode"],
            ItemName=r["ItemName"],
            Pack=r["Pack"],
            BatchNo=r["BatchNo"],
            Expiry=to_iso(r["Expiry"]),
            Quantity=float(r["QTY"] or 0),
            SellRate=float(r["SellRate"] or 0),
            MRP=float(r["MRP"] or 0),
            blocked=bool(r["blocked"]),
        )
        for r in rows
    ]


# ------------------------------------------------------------------
# NEAR-EXPIRY BATCHES (30 / 60 / 90 day windows, chosen by the caller)
# ------------------------------------------------------------------
@router.get("/near-expiry", response_model=list[NearExpiryBatch])
async def get_near_expiry_stock(
    days: int = Query(
        30,
        ge=1,
        le=365,
        description="Show batches expiring within this many days, e.g. 30, 60, or 90",
    ),
    current_user: auth.UserInDB = Depends(auth.get_current_user),
):
    """
    Returns batches of this MR's brand expiring within the given
    window, soonest-expiring first. Blocked batches and batches with
    zero quantity are excluded, since there's nothing sellable to act on.
    """
    company_code = current_user.erp_code
    cutoff_date = date.today() + timedelta(days=days)

    rows = run_query(
        """
        SELECT p.itemCode, p.Name AS ItemName,
               b.[Batch No] AS BatchNo, b.Expiry, b.QTY,
               b.[Sell Rate] AS SellRate, b.MRP
        FROM ProductMaster p
        INNER JOIN [Batch Details] b ON b.ItemCode = p.itemCode
        WHERE p.Company_Code = ?
          AND b.Expiry <= ?
          AND b.QTY > 0
          AND (b.blocked IS NULL OR b.blocked = 0)
        ORDER BY b.Expiry ASC
        """,
        (company_code, cutoff_date),
    )

    today = date.today()
    results = []
    for r in rows:
        expiry_date = to_date(r["Expiry"])
        days_left = (expiry_date - today).days if expiry_date else 0
        results.append(
            NearExpiryBatch(
                ItemCode=r["itemCode"],
                ItemName=r["ItemName"],
                BatchNo=r["BatchNo"],
                Expiry=to_iso(r["Expiry"]),
                days_to_expiry=days_left,
                Quantity=float(r["QTY"] or 0),
                SellRate=float(r["SellRate"] or 0),
                MRP=float(r["MRP"] or 0),
            )
        )
    return results


# ------------------------------------------------------------------
# BRAND SALES
# ------------------------------------------------------------------
@router.get("/sales", response_model=list[MRInvoiceSummary])
async def get_brand_sales(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """
    Returns every Sale_Bill whose Company_Code matches this MR's brand,
    joined with the customer's name from General_Ledger for readability,
    most recent first.
    """
    company_code = current_user.erp_code

    rows = run_query(
        """
        SELECT sb.Bill_No, sb.Bill_Date, sb.CustomerCode, gl.Name AS CustomerName,
               sb.Amount, sb.AdjAmount
        FROM Sale_Bill sb
        LEFT JOIN General_Ledger gl ON gl.GlCode = sb.CustomerCode
        WHERE sb.Company_Code = ?
        ORDER BY sb.Bill_Date DESC
        """,
        (company_code,),
    )

    return [
        MRInvoiceSummary(
            Bill_No=r["Bill_No"],
            Bill_Date=to_iso(r["Bill_Date"]),
            CustomerCode=r["CustomerCode"],
            CustomerName=r["CustomerName"],
            Amount=float(r["Amount"] or 0),
            AdjAmount=float(r["AdjAmount"] or 0),
            outstanding=round(float(r["Amount"] or 0) - float(r["AdjAmount"] or 0), 2),
        )
        for r in rows
    ]


@router.get("/sales/{bill_no}", response_model=MRInvoiceDetail)
async def get_brand_sale_detail(
    bill_no: int,
    current_user: auth.UserInDB = Depends(auth.get_current_user),
):
    """
    Returns one invoice's header plus itemized products, but ONLY if
    that bill's Company_Code matches this MR's brand - otherwise 404.
    This stops an MR from viewing another company's bill by guessing a
    bill number.
    """
    company_code = current_user.erp_code

    header_rows = run_query(
        """
        SELECT sb.Bill_No, sb.Bill_Date, sb.CustomerCode, gl.Name AS CustomerName,
               sb.Amount, sb.AdjAmount
        FROM Sale_Bill sb
        LEFT JOIN General_Ledger gl ON gl.GlCode = sb.CustomerCode
        WHERE sb.Bill_No = ? AND sb.Company_Code = ?
        """,
        (bill_no, company_code),
    )
    if not header_rows:
        raise HTTPException(status_code=404, detail="Invoice not found")
    header = header_rows[0]

    detail_rows = run_query(
        """
        SELECT ItemCode, BatchNo, Expiry, Quantity, Rate, Amount
        FROM Sale_Bill_Details
        WHERE Bill_No = ?
        """,
        (bill_no,),
    )

    return MRInvoiceDetail(
        Bill_No=header["Bill_No"],
        Bill_Date=to_iso(header["Bill_Date"]),
        CustomerCode=header["CustomerCode"],
        CustomerName=header["CustomerName"],
        Amount=float(header["Amount"] or 0),
        AdjAmount=float(header["AdjAmount"] or 0),
        items=[
            MRInvoiceLineItem(
                ItemCode=d["ItemCode"],
                BatchNo=d["BatchNo"],
                Expiry=to_iso(d["Expiry"]),
                Quantity=float(d["Quantity"] or 0),
                Rate=float(d["Rate"] or 0),
                Amount=float(d["Amount"] or 0),
            )
            for d in detail_rows
        ],
    )


# ------------------------------------------------------------------
# BRAND RECEIVABLES (unpaid dues, per customer, for this brand only)
# ------------------------------------------------------------------
@router.get("/receivables", response_model=list[ReceivableByCustomer])
async def get_brand_receivables(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """
    Aggregates unpaid dues PER CUSTOMER, for bills belonging to this
    MR's brand only, highest outstanding first.

    This is calculated straight from Sale_Bill (SUM(Amount - AdjAmount))
    rather than from the customer's overall General_Ledger balance,
    because a pharmacy's ledger balance mixes every brand it buys from
    together - an MR should only ever see what's owed on THEIR products,
    not a competitor's.
    """
    company_code = current_user.erp_code

    rows = run_query(
        """
        SELECT sb.CustomerCode, gl.Name AS CustomerName,
               SUM(sb.Amount) AS TotalBilled,
               SUM(sb.AdjAmount) AS TotalAdjusted,
               SUM(sb.Amount - sb.AdjAmount) AS TotalOutstanding
        FROM Sale_Bill sb
        LEFT JOIN General_Ledger gl ON gl.GlCode = sb.CustomerCode
        WHERE sb.Company_Code = ?
        GROUP BY sb.CustomerCode, gl.Name
        HAVING SUM(sb.Amount - sb.AdjAmount) > 0
        ORDER BY SUM(sb.Amount - sb.AdjAmount) DESC
        """,
        (company_code,),
    )

    return [
        ReceivableByCustomer(
            CustomerCode=r["CustomerCode"],
            CustomerName=r["CustomerName"],
            total_billed=float(r["TotalBilled"] or 0),
            total_adjusted=float(r["TotalAdjusted"] or 0),
            total_outstanding=round(float(r["TotalOutstanding"] or 0), 2),
        )
        for r in rows
    ]
