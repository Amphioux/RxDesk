"""
RxDesk Backend - routers/pharmacy.py
=======================================
Endpoints exclusively for the PHARMACY role.

SECURITY RULE (read this before adding new endpoints here):
Every query below is scoped to current_user.erp_code (the pharmacy's
own GlCode), which comes from the verified JWT - NEVER from a
client-supplied parameter. This is what stops one pharmacy from ever
viewing another pharmacy's ledger, invoices, or returns, even if they
guess another customer's GlCode or bill number. This pattern is called
preventing "IDOR" (Insecure Direct Object Reference) - always filter
by the identity in the token, never trust an ID from the URL alone
without also checking it belongs to the caller.

Per RxDesk's RBAC rules, pharmacies are STRICTLY BLOCKED from seeing
inventory/stock data - there are simply no stock endpoints in this
file, and the router-level dependency below only ever allows the
PHARMACY role in.
"""

from fastapi import APIRouter, Depends, HTTPException

from .. import auth
from ..database import run_query, to_iso
from ..models import (
    InvoiceDetail,
    InvoiceLineItem,
    InvoiceSummary,
    LedgerSummary,
    LedgerTransaction,
    ReturnDetail,
    ReturnLineItem,
    ReturnSummary,
)

router = APIRouter(
    prefix="/api/pharmacy",
    tags=["Pharmacy"],
    # This dependency runs on EVERY route in this file. Anyone who
    # isn't logged in as PHARMACY gets a 403 before the route code
    # even executes.
    dependencies=[Depends(auth.require_role(["PHARMACY"]))],
)


# ------------------------------------------------------------------
# MY LEDGER
# ------------------------------------------------------------------
@router.get("/ledger", response_model=LedgerSummary)
async def get_my_ledger(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """
    Returns the pharmacy's full statement: account summary from
    General_Ledger, plus every transaction from Account Transaction,
    with a running balance calculated chronologically (oldest first).
    """
    gl_code = current_user.erp_code

    account_rows = run_query(
        """
        SELECT GlCode, Name, [Current Balance] AS CurrentBalance,
               [Cr Days] AS CrDays, [Cr Limit] AS CrLimit
        FROM General_Ledger
        WHERE GlCode = ?
        """,
        (gl_code,),
    )
    if not account_rows:
        raise HTTPException(
            status_code=404,
            detail="No General_Ledger account found for this login's GlCode. Contact your Distributor Admin.",
        )
    account = account_rows[0]

    txn_rows = run_query(
        """
        SELECT [Date], [Type], [Document], [Amount], [Dr/Cr] AS DrCr, [Narration]
        FROM [Account Transaction]
        WHERE GLCode = ?
        ORDER BY [Date] ASC
        """,
        (gl_code,),
    )

    # Build the running balance in Python, oldest transaction first.
    # Convention: Dr (debit) increases what the pharmacy owes (e.g. a
    # new sale); Cr (credit) decreases it (e.g. a payment received).
    running_balance = 0.0
    transactions = []
    for row in txn_rows:
        amount = float(row["Amount"] or 0)
        dr_cr = (row["DrCr"] or "").strip().upper()
        if dr_cr.startswith("D"):
            running_balance += amount
        else:
            running_balance -= amount

        transactions.append(
            LedgerTransaction(
                Date=to_iso(row["Date"]),
                Type=row["Type"],
                Document=row["Document"],
                Amount=amount,
                DrCr=row["DrCr"],
                Narration=row["Narration"],
                running_balance=round(running_balance, 2),
            )
        )

    return LedgerSummary(
        GlCode=account["GlCode"],
        Name=account["Name"],
        current_balance=float(account["CurrentBalance"] or 0),
        credit_days=account["CrDays"],
        credit_limit=float(account["CrLimit"]) if account["CrLimit"] is not None else None,
        transactions=transactions,
    )


# ------------------------------------------------------------------
# MY INVOICES
# ------------------------------------------------------------------
@router.get("/invoices", response_model=list[InvoiceSummary])
async def list_my_invoices(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """Lists every Sale_Bill issued to this pharmacy, most recent first."""
    gl_code = current_user.erp_code

    rows = run_query(
        """
        SELECT Bill_No, Bill_Date, Amount, AdjAmount
        FROM Sale_Bill
        WHERE CustomerCode = ?
        ORDER BY Bill_Date DESC
        """,
        (gl_code,),
    )

    return [
        InvoiceSummary(
            Bill_No=r["Bill_No"],
            Bill_Date=to_iso(r["Bill_Date"]),
            Amount=float(r["Amount"] or 0),
            AdjAmount=float(r["AdjAmount"] or 0),
            outstanding=round(float(r["Amount"] or 0) - float(r["AdjAmount"] or 0), 2),
        )
        for r in rows
    ]


@router.get("/invoices/{bill_no}", response_model=InvoiceDetail)
async def get_my_invoice_detail(
    bill_no: int,
    current_user: auth.UserInDB = Depends(auth.get_current_user),
):
    """
    Returns one invoice's header plus its full itemized product list
    from Sale_Bill_Details. The WHERE clause checks BOTH the bill
    number AND that it belongs to this pharmacy's GlCode - so guessing
    another pharmacy's bill number returns 404, not their data.
    """
    gl_code = current_user.erp_code

    header_rows = run_query(
        """
        SELECT Bill_No, Bill_Date, Amount, AdjAmount
        FROM Sale_Bill
        WHERE Bill_No = ? AND CustomerCode = ?
        """,
        (bill_no, gl_code),
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

    return InvoiceDetail(
        Bill_No=header["Bill_No"],
        Bill_Date=to_iso(header["Bill_Date"]),
        Amount=float(header["Amount"] or 0),
        AdjAmount=float(header["AdjAmount"] or 0),
        items=[
            InvoiceLineItem(
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
# MY RETURNS (regular sales returns + expired-stock credit notes)
# ------------------------------------------------------------------
@router.get("/returns", response_model=list[ReturnSummary])
async def list_my_returns(current_user: auth.UserInDB = Depends(auth.get_current_user)):
    """
    Combines Sale_Return (regular returns) and Sale_Exp_Return (expired
    stock credit notes) into one list, tagged by return_type so the
    Flutter app can show them differently, sorted newest first.
    """
    gl_code = current_user.erp_code

    sale_returns = run_query(
        """
        SELECT Debit_Note_No, DNote_Date, Ref_Bill_No, Amount, AdjAmount
        FROM Sale_Return
        WHERE CustomerCode = ?
        """,
        (gl_code,),
    )
    exp_returns = run_query(
        """
        SELECT Debit_Note_No, DNote_Date, Ref_Bill_No, Amount, AdjAmount
        FROM Sale_Exp_Return
        WHERE CustomerCode = ?
        """,
        (gl_code,),
    )

    results = [
        ReturnSummary(
            Debit_Note_No=r["Debit_Note_No"],
            DNote_Date=to_iso(r["DNote_Date"]),
            Ref_Bill_No=r["Ref_Bill_No"],
            Amount=float(r["Amount"] or 0),
            AdjAmount=float(r["AdjAmount"] or 0),
            return_type="SALE_RETURN",
        )
        for r in sale_returns
    ] + [
        ReturnSummary(
            Debit_Note_No=r["Debit_Note_No"],
            DNote_Date=to_iso(r["DNote_Date"]),
            Ref_Bill_No=r["Ref_Bill_No"],
            Amount=float(r["Amount"] or 0),
            AdjAmount=float(r["AdjAmount"] or 0),
            return_type="EXPIRED_RETURN",
        )
        for r in exp_returns
    ]

    results.sort(key=lambda x: x.DNote_Date or "", reverse=True)
    return results


@router.get("/returns/{debit_note_no}", response_model=ReturnDetail)
async def get_my_return_detail(
    debit_note_no: int,
    return_type: str,
    current_user: auth.UserInDB = Depends(auth.get_current_user),
):
    """
    Returns one return/credit-note's header plus its itemized product
    list. Pass return_type=SALE_RETURN or return_type=EXPIRED_RETURN as
    a query parameter (the /returns list above tells you which one each
    entry is).

    SECURITY NOTE: return_type is checked against a hardcoded whitelist
    below, and the resulting table name comes from a fixed Python dict -
    never directly from the user's raw string. Never build a SQL table
    or column name by directly interpolating client input; only ever
    pick from a vetted, hardcoded set of known-safe names like this.
    """
    gl_code = current_user.erp_code

    table_map = {
        "SALE_RETURN": ("Sale_Return", "Sale_Return_Details"),
        "EXPIRED_RETURN": ("Sale_Exp_Return", "Sale_Exp_Return_Details"),
    }
    if return_type not in table_map:
        raise HTTPException(
            status_code=400,
            detail="return_type must be SALE_RETURN or EXPIRED_RETURN",
        )
    header_table, detail_table = table_map[return_type]

    header_rows = run_query(
        f"""
        SELECT Debit_Note_No, DNote_Date, Ref_Bill_No, Amount, AdjAmount
        FROM {header_table}
        WHERE Debit_Note_No = ? AND CustomerCode = ?
        """,
        (debit_note_no, gl_code),
    )
    if not header_rows:
        raise HTTPException(status_code=404, detail="Return not found")
    header = header_rows[0]

    detail_rows = run_query(
        f"""
        SELECT ITEMCode AS ItemCode, Batch, Expiry, Quantity, Rate, Amount
        FROM {detail_table}
        WHERE Debit_Note_No = ?
        """,
        (debit_note_no,),
    )

    return ReturnDetail(
        Debit_Note_No=header["Debit_Note_No"],
        DNote_Date=to_iso(header["DNote_Date"]),
        Ref_Bill_No=header["Ref_Bill_No"],
        Amount=float(header["Amount"] or 0),
        AdjAmount=float(header["AdjAmount"] or 0),
        return_type=return_type,
        items=[
            ReturnLineItem(
                ItemCode=d["ItemCode"],
                Batch=d["Batch"],
                Expiry=to_iso(d["Expiry"]),
                Quantity=float(d["Quantity"] or 0),
                Rate=float(d["Rate"] or 0),
                Amount=float(d["Amount"] or 0),
            )
            for d in detail_rows
        ],
    )
