"""
ai_analyzer.py — Claude-powered document analysis.

Analyzes documents for:
  - Classification (domain, category)
  - Summary generation
  - Action item extraction
  - Date and expiration detection
  - Structured data extraction
"""

import json
import logging
from typing import Optional

import anthropic
from pydantic import BaseModel

from config import get_settings
from constants import DOMAINS, CATEGORIES

logger = logging.getLogger(__name__)

CURRENT_PROMPT_VERSION = 2

ANALYSIS_SYSTEM_PROMPT = """You are LifeOS, a personal document management AI. Analyze the document and return structured JSON.

## Domains and Categories

""" + "\n".join(
    f"- **{domain}**: {', '.join(cats)}"
    for domain, cats in CATEGORIES.items()
) + """

## Instructions

1. **Classify** the document into exactly one domain and one category from the lists above.
2. **Title**: Generate a short, descriptive title (5-10 words) that captures what this document is. Examples: "Quest Diagnostics Blood Panel March 2024", "State Farm Auto Policy Renewal", "Bristol Veterinary Rabies Certificate". Do not use the filename.
3. **Summarize** in 1-3 sentences. Be specific — include names, dates, amounts, and key details.
3. **Extract dates**:
   - `document_date`: The date ON the document (when it was written/issued). ISO YYYY-MM-DD.
   - `expiration_date`: If the document expires, renews, or has a deadline. ISO YYYY-MM-DD.
4. **Extract action items**: Only real, actionable tasks with clear next steps. Include due dates when stated.
5. **Extract structured data**: Key fields like amounts, account numbers, provider names, policy numbers, etc.
6. **Assign tags**: 2-5 relevant keywords for search.
7. **Confidence**: 0.0-1.0 for how certain you are about the domain/category classification.

## Metrics Extraction

For certain document types, extract numeric measurements into the `metrics` array:
- **medical** (lab_result, test_result): Extract lab values — glucose, total_cholesterol, LDL, HDL, HbA1c, triglycerides, blood_pressure, weight, BMI, TSH, vitamin_d, creatinine, etc. Use the test/service date as `recorded_at`.
- **auto** (service_record, inspection): Extract mileage readings as metric_type "mileage".
- Each metric object: `{"metric_type": "glucose", "value": 102, "value_text": null, "recorded_at": "2024-03-15"}`
- Use `value` (number) for pure numeric values. Use `value_text` (string) for compound values like blood pressure "120/80".
- `recorded_at` should be the test/service date in ISO YYYY-MM-DD format, or null if unknown.
- metric_type should be lowercase_snake_case (e.g. "total_cholesterol", "blood_pressure", "hba1c").

## Subject Matching

Determine who or what this document is about and set `subject_hint`:
- For **medical**, **financial**, **legal**, **insurance** documents: set subject_hint to "Dave" (the system owner) unless the document clearly names a different person.
- For **vet** documents: extract the pet's name (e.g. "Luna", "Max").
- For **auto** documents: extract the vehicle description (e.g. "2022 Toyota Tacoma").
- For **home** documents: extract the property address if present.
- If uncertain, set subject_hint to null.

## Response Format (updated)

Return ONLY valid JSON (no markdown, no explanation):
{
  "title": "Quest Diagnostics Blood Panel March 2024",
  "summary": "...",
  "domain": "medical",
  "category": "lab_result",
  "document_date": "2024-03-15",
  "expiration_date": null,
  "confidence": 0.92,
  "tags": ["blood work", "cholesterol"],
  "subject_hint": "Dave",
  "metrics": [
    {"metric_type": "glucose", "value": 102, "value_text": null, "recorded_at": "2024-03-15"},
    {"metric_type": "blood_pressure", "value": null, "value_text": "120/80", "recorded_at": "2024-03-15"}
  ],
  "action_items": [
    {"title": "Schedule follow-up", "description": "...", "due_date": "2024-06-15", "priority": "medium"}
  ],
  "extracted_data": {"provider_name": "Dr. Smith", "facility": "Castle Rock Medical Center"}
}

## Rules
- Only assign domains/categories from the provided lists
- Only extract action items that are clearly stated or strongly implied tasks
- Dates must be ISO format (YYYY-MM-DD) or null
- If text is too short or unclear, set confidence low and use your best guess
- Do not invent information not present in the document
- priority must be one of: low, medium, high
- metrics array can be empty if no measurable values found
- subject_hint should be null if the subject cannot be determined"""


class AnalysisResult(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    domain: Optional[str] = None
    category: Optional[str] = None
    document_date: Optional[str] = None
    expiration_date: Optional[str] = None
    confidence: float = 0.0
    tags: list[str] = []
    action_items: list[dict] = []
    action_items_raw: Optional[list] = None  # raw JSON for DB storage
    extracted_data: Optional[dict] = None
    subject_hint: Optional[str] = None
    metrics: list[dict] = []
    prompt_version: int = CURRENT_PROMPT_VERSION


async def analyze_document(
    text: str,
    filename: str = "",
    mime_type: str = "",
    existing_domain: str = None,
    existing_category: str = None,
) -> AnalysisResult:
    """Analyze document text with Claude and return structured results.

    Never raises — returns a fallback result on any failure.
    """
    settings = get_settings()
    fallback = AnalysisResult()

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key configured, skipping analysis")
        return fallback

    if not text or len(text.strip()) < 50:
        logger.info("Text too short for analysis, skipping")
        return fallback

    try:
        # Truncate to ~8000 tokens (~32000 chars)
        truncated = text[:32000]

        # Build user message
        parts = []
        if filename:
            parts.append(f"Filename: {filename}")
        if mime_type:
            parts.append(f"File type: {mime_type}")
        if existing_domain:
            parts.append(f"User-assigned domain: {existing_domain}")
        if existing_category:
            parts.append(f"User-assigned category: {existing_category}")
        parts.append(f"\nDocument text:\n{truncated}")

        user_message = "\n".join(parts)

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

        message = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            temperature=0.0,
            system=ANALYSIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        response_text = message.content[0].text.strip()

        # Strip markdown code fences if present
        if response_text.startswith("```"):
            response_text = response_text.split("\n", 1)[-1] if "\n" in response_text else response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3].strip()

        result = json.loads(response_text)

        # Validate domain
        ai_domain = result.get("domain")
        if ai_domain and ai_domain not in DOMAINS:
            ai_domain = None

        # Validate category
        ai_category = result.get("category")
        all_cats = [c for cats in CATEGORIES.values() for c in cats]
        if ai_category and ai_category not in all_cats:
            ai_category = None

        # Validate confidence
        confidence = float(result.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        # Validate dates
        doc_date = _validate_date(result.get("document_date"))
        exp_date = _validate_date(result.get("expiration_date"))

        # Validate action items
        action_items = []
        valid_priorities = {"low", "medium", "high"}
        for item in result.get("action_items", []):
            if not isinstance(item, dict):
                continue
            title = item.get("title", "").strip()
            if not title:
                continue
            priority = item.get("priority", "medium")
            if priority not in valid_priorities:
                priority = "medium"
            clean_item = {
                "title": title,
                "description": item.get("description"),
                "due_date": _validate_date(item.get("due_date")),
                "priority": priority,
            }
            action_items.append(clean_item)

        # Tags
        tags = [str(t).strip().lower() for t in result.get("tags", []) if t][:10]

        # Subject hint
        subject_hint = result.get("subject_hint")
        if subject_hint:
            subject_hint = str(subject_hint).strip() or None

        # Validate metrics
        metrics = []
        for m in result.get("metrics", []):
            if not isinstance(m, dict):
                continue
            mt = str(m.get("metric_type", "")).strip().lower()
            if not mt:
                continue
            value = m.get("value")
            value_text = m.get("value_text")
            if value is not None:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    value = None
            if value is None and not value_text:
                continue  # need at least one value
            recorded_at = _validate_date(m.get("recorded_at"))
            metrics.append({
                "metric_type": mt,
                "value": value,
                "value_text": str(value_text) if value_text else None,
                "recorded_at": recorded_at,
            })

        # Title
        ai_title = result.get("title")
        if ai_title:
            ai_title = str(ai_title).strip()[:200] or None

        return AnalysisResult(
            title=ai_title,
            summary=result.get("summary"),
            domain=ai_domain,
            category=ai_category,
            document_date=doc_date,
            expiration_date=exp_date,
            confidence=confidence,
            tags=tags,
            action_items=action_items,
            action_items_raw=action_items,
            extracted_data=result.get("extracted_data"),
            subject_hint=subject_hint,
            metrics=metrics,
            prompt_version=CURRENT_PROMPT_VERSION,
        )

    except json.JSONDecodeError as e:
        logger.warning(f"AI analysis returned invalid JSON: {e}")
        return fallback
    except anthropic.APIError as e:
        logger.warning(f"AI analysis API error: {e}")
        return fallback
    except Exception as e:
        logger.warning(f"AI analysis unexpected error: {type(e).__name__}: {e}")
        return fallback


def _validate_date(value) -> Optional[str]:
    """Validate an ISO date string. Returns the string if valid, else None."""
    if not value:
        return None
    try:
        from datetime import date as dt_date
        dt_date.fromisoformat(str(value).strip())
        return str(value).strip()
    except (ValueError, AttributeError):
        return None
