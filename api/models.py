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
    # Full multi-subject set; first entry is treated as the primary. When
    # provided, replaces the entire subject set for this document.
    subject_ids: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    review_status: Optional[str] = None
    document_date: Optional[str] = None
    expiration_date: Optional[str] = None
    summary: Optional[str] = None
    notes: Optional[str] = None
    clear_suggestion: Optional[bool] = None
    # Phase 6: link this document to a structured_record (typically a
    # vehicle). Pass None / empty string to clear; pass a UUID to set.
    linked_record_id: Optional[str] = None
    # Sentinel: only honor linked_record_id from the payload if explicitly
    # provided. Pydantic v2 model_fields_set handles this for us.


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
