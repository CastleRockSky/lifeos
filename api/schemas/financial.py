"""
schemas/financial.py — Pydantic models for financial structured_records.

Shape definitions for record_type values: bank_account, credit_account, loan,
recurring_expense, tax_item.

All fields optional so partial AI extraction (e.g. a credit-card statement that
mentions balance + due date but not the APR) still produces a valid record.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class BankAccount(_Base):
    institution: Optional[str] = None
    account_type: Optional[str] = None        # checking, savings, money_market, cd, investment
    last_four: Optional[str] = None
    balance: Optional[float] = None
    balance_date: Optional[date] = None
    monthly_fee: Optional[float] = None
    notes: Optional[str] = None


class CreditAccount(_Base):
    creditor: Optional[str] = None
    last_four: Optional[str] = None
    credit_limit: Optional[float] = None
    current_balance: Optional[float] = None
    balance_date: Optional[date] = None
    apr: Optional[float] = None
    minimum_payment: Optional[float] = None
    payment_due_date: Optional[date] = None
    autopay: Optional[bool] = None
    autopay_amount: Optional[str] = None      # "minimum", "statement_balance", or fixed
    status: Optional[str] = Field(default="active")  # active, closed, frozen
    notes: Optional[str] = None


class Loan(_Base):
    lender: Optional[str] = None
    loan_type: Optional[str] = None           # auto, mortgage, student, personal, business
    original_amount: Optional[float] = None
    current_balance: Optional[float] = None
    balance_date: Optional[date] = None
    interest_rate: Optional[float] = None
    monthly_payment: Optional[float] = None
    payment_due_day: Optional[int] = Field(default=None, ge=1, le=31)
    remaining_payments: Optional[int] = None
    payoff_date: Optional[date] = None
    collateral: Optional[str] = None
    autopay: Optional[bool] = None
    status: Optional[str] = Field(default="active")  # active, paid_off, defaulted
    notes: Optional[str] = None


class RecurringExpense(_Base):
    name: Optional[str] = None
    amount: Optional[float] = None
    frequency: Optional[str] = Field(default="monthly")  # weekly, monthly, quarterly, yearly
    due_day: Optional[int] = Field(default=None, ge=1, le=31)
    category: Optional[str] = None            # utilities, subscription, insurance, etc.
    autopay: Optional[bool] = None
    account: Optional[str] = None             # free-text reference to the paying account
    status: Optional[str] = Field(default="active")  # active, cancelled, paused
    notes: Optional[str] = None


class TaxItem(_Base):
    tax_year: Optional[int] = None
    item_type: Optional[str] = None           # deadline, payment, document, refund
    description: Optional[str] = None
    due_date: Optional[date] = None
    amount: Optional[float] = None
    status: Optional[str] = Field(default="pending")  # pending, paid, filed, refunded
    notes: Optional[str] = None
