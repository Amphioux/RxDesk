"""
RxDesk Backend - models.py
=============================
Pydantic response schemas for ERP-derived data, shared across the
Pharmacy and MR routers. Kept separate from auth.py's models to keep
concerns clean: auth.py answers "who is logged in", this file answers
"what does ERP data look like once it reaches the Flutter app".
"""

from typing import List, Optional

from pydantic import BaseModel


# ---------------------- PHARMACY: Ledger ----------------------
class LedgerTransaction(BaseModel):
    Date: Optional[str]
    Type: Optional[str]
    Document: Optional[str]
    Amount: float
    DrCr: str  # "Dr" (increases what the pharmacy owes) or "Cr" (payment received)
    Narration: Optional[str]
    running_balance: float


class LedgerSummary(BaseModel):
    GlCode: str
    Name: str
    current_balance: float
    credit_days: Optional[int]
    credit_limit: Optional[float]
    transactions: List[LedgerTransaction]


# ---------------------- PHARMACY: Invoices ----------------------
class InvoiceSummary(BaseModel):
    Bill_No: int
    Bill_Date: Optional[str]
    Amount: float
    AdjAmount: float
    outstanding: float


class InvoiceLineItem(BaseModel):
    ItemCode: str
    BatchNo: Optional[str]
    Expiry: Optional[str]
    Quantity: float
    Rate: float
    Amount: float


class InvoiceDetail(BaseModel):
    Bill_No: int
    Bill_Date: Optional[str]
    Amount: float
    AdjAmount: float
    items: List[InvoiceLineItem]


# ---------------------- PHARMACY: Returns ----------------------
class ReturnSummary(BaseModel):
    Debit_Note_No: int
    DNote_Date: Optional[str]
    Ref_Bill_No: Optional[int]
    Amount: float
    AdjAmount: float
    return_type: str  # "SALE_RETURN" or "EXPIRED_RETURN"


class ReturnLineItem(BaseModel):
    ItemCode: str
    Batch: Optional[str]
    Expiry: Optional[str]
    Quantity: float
    Rate: float
    Amount: float


class ReturnDetail(BaseModel):
    Debit_Note_No: int
    DNote_Date: Optional[str]
    Ref_Bill_No: Optional[int]
    Amount: float
    AdjAmount: float
    return_type: str
    items: List[ReturnLineItem]


# ---------------------- MR: Stock ----------------------
class StockBatch(BaseModel):
    ItemCode: str
    ItemName: str
    Pack: Optional[str]
    BatchNo: str
    Expiry: Optional[str]
    Quantity: float
    SellRate: float
    MRP: float
    blocked: bool


class NearExpiryBatch(BaseModel):
    ItemCode: str
    ItemName: str
    BatchNo: str
    Expiry: Optional[str]
    days_to_expiry: int
    Quantity: float
    SellRate: float
    MRP: float


# ---------------------- MR: Sales & Receivables ----------------------
class MRInvoiceSummary(BaseModel):
    Bill_No: int
    Bill_Date: Optional[str]
    CustomerCode: str
    CustomerName: Optional[str]
    Amount: float
    AdjAmount: float
    outstanding: float


class MRInvoiceLineItem(BaseModel):
    ItemCode: str
    BatchNo: Optional[str]
    Expiry: Optional[str]
    Quantity: float
    Rate: float
    Amount: float


class MRInvoiceDetail(BaseModel):
    Bill_No: int
    Bill_Date: Optional[str]
    CustomerCode: str
    CustomerName: Optional[str]
    Amount: float
    AdjAmount: float
    items: List[MRInvoiceLineItem]


class ReceivableByCustomer(BaseModel):
    CustomerCode: str
    CustomerName: Optional[str]
    total_billed: float
    total_adjusted: float
    total_outstanding: float


# ---------------------- ADMIN: Dashboard ----------------------
class CompanyExpiryRisk(BaseModel):
    Company_Code: str
    CompanyName: Optional[str]
    at_risk_value: float
    batch_count: int


class AdminDashboardSummary(BaseModel):
    total_receivables: float
    total_customers_with_dues: int
    total_returns_amount: float
    total_return_count: int
    expiry_risk_window_days: int
    stock_expiry_risk_value: float
    expiry_risk_by_company: List[CompanyExpiryRisk]
