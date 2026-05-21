"""
models.py — Pydantic request/response models.
"""

from typing import Optional
from pydantic import BaseModel


class SubjectCreate(BaseModel):
    name: str
    type: str = "person"
    profile_data: dict = {}
    is_primary: bool = False


class SubjectUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    profile_data: Optional[dict] = None


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    domain: Optional[str] = None
    category: Optional[str] = None
    subject_id: Optional[str] = None
    tags: Optional[list[str]] = None
    review_status: Optional[str] = None
    document_date: Optional[str] = None
    expiration_date: Optional[str] = None
    summary: Optional[str] = None
    notes: Optional[str] = None
    clear_suggestion: Optional[bool] = None


class ActionItemCreate(BaseModel):
    title: str
    description: Optional[str] = None
    domain: Optional[str] = None
    subject_id: Optional[str] = None
    due_date: Optional[str] = None
    priority: str = "medium"


class ActionItemUpdate(BaseModel):
    status: Optional[str] = None
    due_date: Optional[str] = None
    notes: Optional[str] = None
    title: Optional[str] = None
    priority: Optional[str] = None


class AskRequest(BaseModel):
    question: str
    domain: Optional[str] = None
