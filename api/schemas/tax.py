"""
schemas/tax.py — Pydantic models for tax structured_records.

Tax items track deadlines, estimated payments, returns filed, and
correspondence with the IRS / state tax authorities.
"""

from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class TaxItem(_Base):
    tax_year: Optional[int] = None
    item_type: Optional[str] = None           # deadline, payment, document, refund, return
    description: Optional[str] = None
    due_date: Optional[date] = None
    amount: Optional[float] = None
    status: Optional[str] = Field(default="pending")  # pending, paid, filed, refunded
    jurisdiction: Optional[str] = None        # federal, CO, etc.
    confirmation_number: Optional[str] = None
    notes: Optional[str] = None
