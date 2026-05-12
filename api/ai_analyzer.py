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

CURRENT_PROMPT_VERSION = 6

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
- **financial** (credit_card_statement, bank_statement, investment_statement): Extract balances and utilization as metrics: `credit_card_balance`, `bank_account_balance`, `investment_balance`, `net_worth`, `total_credit_utilization` (percent). Use the statement date as `recorded_at`. For credit_card_statement specifically, extract one `credit_card_balance` per card.
- **vet** (vet_visit_note, lab_result, weight check): Extract `pet_weight` (lbs), `pet_temperature` (°F), `pet_body_condition_score` (1-9). Use the visit date as `recorded_at`.
- **auto** (service_record, inspection): Extract mileage readings as metric_type "mileage".
- Each metric object: `{"metric_type": "glucose", "value": 102, "value_text": null, "recorded_at": "2024-03-15"}`
- Use `value` (number) for pure numeric values. Use `value_text` (string) for compound values like blood pressure "120/80".
- `recorded_at` should be the test/service date in ISO YYYY-MM-DD format, or null if unknown.
- metric_type should be lowercase_snake_case (e.g. "total_cholesterol", "blood_pressure", "hba1c").

## Structured Records Extraction

For documents that describe ongoing entities (medications, providers, conditions, vaccinations, lab panels), populate the `structured_records` array. These create reusable records the user can query later — they're not the same as one-off action items or metrics.

Supported record_type values and their data shapes:

- **medication** (from prescriptions, refill receipts, med lists):
  `{"name": "Lisinopril", "dose": "10mg", "frequency": "1x daily", "time_of_day": "morning", "prescriber": "Dr. Sarah Chen", "pharmacy": "King Soopers", "rx_number": "RX-7891234", "start_date": "2024-01-15", "refill_date": "2025-04-01", "quantity": 90, "refills_remaining": 3, "indication": "Hypertension", "status": "active", "notes": null}`

- **provider** (from referrals, visit notes that introduce a new provider):
  `{"name": "Dr. Sarah Chen", "specialty": "Internal Medicine", "practice": "Castle Rock Medical Group", "phone": "303-555-0100", "portal_url": "https://...", "address": "...", "next_appointment": "2025-06-15", "notes": null}`

- **condition** (from diagnosis notes, problem lists):
  `{"name": "Essential Hypertension", "icd10": "I10", "diagnosed_date": "2024-01-15", "diagnosing_provider": "Dr. Sarah Chen", "status": "active", "management": "Medication + lifestyle", "notes": null}`

- **vaccination** (from immunization records):
  `{"name": "Influenza", "date_administered": "2024-10-15", "provider": "Walgreens", "lot_number": "FL2024-789", "next_due": "2025-10-01", "series": null, "dose_number": null}`

- **lab_result_set** (from lab reports — pair this with the `metrics` array, which holds the same numeric values for trending):
  `{"lab": "Quest Diagnostics", "ordering_provider": "Dr. Sarah Chen", "date": "2025-03-15", "results": [{"test": "Total Cholesterol", "value": 210, "unit": "mg/dL", "reference_range": "< 200", "flag": "high"}]}`

- **bank_account** (from bank statements, account opening docs):
  `{"institution": "Chase", "account_type": "checking", "last_four": "4567", "balance": 5432.10, "balance_date": "2025-03-27", "monthly_fee": 0, "notes": null}`

- **credit_account** (from credit card statements — also emit `metrics`: `credit_card_balance` per card and `total_credit_utilization` if you can compute it):
  `{"creditor": "Chase Sapphire", "last_four": "8901", "credit_limit": 15000, "current_balance": 3200.50, "balance_date": "2025-03-27", "apr": 24.99, "minimum_payment": 75, "payment_due_date": "2025-04-15", "autopay": true, "autopay_amount": "minimum", "notes": null}`

- **loan** (from loan agreements, mortgage / auto / student / personal loan statements):
  `{"lender": "US Bank", "loan_type": "auto", "original_amount": 28000, "current_balance": 18500, "balance_date": "2025-03-27", "interest_rate": 5.9, "monthly_payment": 485, "payment_due_day": 15, "remaining_payments": 42, "payoff_date": "2028-09-15", "collateral": "2023 Toyota Tacoma", "autopay": true, "notes": null}`

- **recurring_expense** (from utility bills, subscription receipts, insurance policy bills):
  `{"name": "Comcast Internet", "amount": 89.99, "frequency": "monthly", "due_day": 22, "category": "utilities", "autopay": true, "account": "Chase Checking *4567", "notes": null}`

- **tax_item** (from IRS notices, estimated-tax voucher reminders, tax filing confirmations):
  `{"tax_year": 2025, "item_type": "deadline", "description": "Q1 Estimated Tax Payment", "due_date": "2025-04-15", "amount": 2500, "status": "pending", "notes": "Federal + Colorado state"}`

- **vehicle** (from registrations, titles, purchase agreements, insurance cards):
  `{"year": 2023, "make": "Toyota", "model": "Tacoma", "trim": "TRD Off-Road", "vin": "JTXXX...", "license_plate": "ABC-1234", "color": "Lunar Rock", "purchase_date": "2023-06-15", "purchase_price": 42000, "current_mileage": 28500, "registration_expiration": "2025-12-31", "notes": null}`

- **service_record** (from service receipts, maintenance invoices — also emit `mileage` metric when present):
  `{"vehicle_record_id": null, "date": "2025-01-15", "mileage": 26000, "service_type": "Oil Change", "provider": "Toyota of Castle Rock", "cost": 72.50, "parts": ["Oil filter", "5qt 0W-20 synthetic"], "notes": "Tire rotation included"}`

- **maintenance_schedule** (from owner's manual extracts, service interval guides — usually a follow-up to a service_record):
  `{"vehicle_record_id": null, "service_type": "Oil Change", "interval_miles": 5000, "interval_months": 6, "last_service_date": "2025-01-15", "last_service_mileage": 26000, "estimated_cost": 75, "notes": "Full synthetic 0W-20"}`

- **property** (from mortgage agreements, property tax bills, deed documents):
  `{"address": "123 Main St, Castle Rock, CO 80104", "type": "single_family", "year_built": 2018, "sqft": 2400, "bedrooms": 4, "bathrooms": 3, "hoa": true, "hoa_monthly": 75, "notes": null}`

- **appliance** (from appliance purchase receipts, warranty registrations, service records):
  `{"name": "HVAC System", "brand": "Lennox", "model": "XC21", "serial": "XXXX", "install_date": "2018-03-15", "warranty_expiration": "2028-03-15", "last_service": "2024-10-15", "service_interval_months": 12, "next_service_due": "2025-10-15", "notes": "Filter size: 20x25x4"}`

- **contractor** (from contractor invoices, estimates — name + trade are the key fields):
  `{"name": "Mike's Plumbing", "trade": "plumbing", "phone": "303-555-0200", "email": "mike@mikesplumbing.com", "license_number": null, "last_used": "2024-08-10", "notes": "Used for water heater install"}`

- **home_maintenance_schedule** (from owner-tracked recurring tasks, often paired with appliance):
  `{"task": "Replace HVAC filter", "interval_months": 3, "last_completed": "2025-01-15", "next_due": "2025-04-15", "estimated_cost": 25, "diy": true, "notes": "20x25x4 MERV 11 from Amazon"}`

- **vet_provider** (from vet visit notes, vaccination records that introduce a clinic):
  `{"name": "Dr. Sarah Reeves", "practice": "Bristol Veterinary Hospital", "species_specialty": "small animal", "phone": "303-555-0300", "address": "...", "next_appointment": "2025-06-15", "notes": null}`

- **pet_medication** (from vet prescriptions, pet pharmacy receipts):
  `{"name": "Apoquel", "dose": "16mg", "frequency": "1x daily", "prescriber": "Dr. Reeves", "pharmacy": "Bristol Vet", "rx_number": null, "start_date": "2024-08-01", "refill_date": "2025-04-15", "quantity": 30, "refills_remaining": 2, "indication": "Allergy itch", "weight_based_dosing": "0.4mg/kg", "status": "active", "notes": null}`

- **pet_vaccination** (from vaccination records, rabies certificates, boarding requirement docs):
  `{"name": "Rabies", "date_administered": "2024-05-12", "provider": "Bristol Vet", "lot_number": "RB-2024-882", "next_due": "2027-05-12", "series": null, "dose_number": null, "required_for_boarding": true, "notes": "3-year vaccine"}`

- **pet_condition** (from diagnosis notes):
  `{"name": "Atopic dermatitis", "diagnosed_date": "2024-08-01", "diagnosing_provider": "Dr. Reeves", "status": "active", "management": "Apoquel + hypoallergenic diet", "notes": null}`

- **preventative_schedule** (flea/tick/heartworm/dental — usually a recurring product the pet takes):
  `{"type": "flea_tick", "product": "NexGard", "dose": "68mg", "frequency": "monthly", "last_administered": "2025-03-01", "next_due": "2025-04-01", "cost_per_dose": 42, "notes": "Chewable — give with food"}`

Rules:
- Only extract records whose subject is genuinely the document's subject (skip records about other people mentioned in passing).
- Use null for unknown fields, but include the field key. Do NOT invent details.
- Dates must be ISO YYYY-MM-DD or null.
- For lab_result_set, every numeric result should ALSO appear in the `metrics` array using a snake_case metric_type.

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
  "structured_records": [
    {"record_type": "lab_result_set", "data": {"lab": "Quest Diagnostics", "date": "2024-03-15", "results": [{"test": "Glucose", "value": 102, "unit": "mg/dL", "reference_range": "70-99", "flag": "high"}]}}
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
- structured_records array can be empty if no reusable entities are described
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
    structured_records: list[dict] = []
    prompt_version: int = CURRENT_PROMPT_VERSION


async def analyze_document(
    text: str,
    filename: str = "",
    mime_type: str = "",
    existing_domain: str = None,
    existing_category: str = None,
    extra_context: str = "",
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
        if extra_context:
            parts.append(f"Source context:\n{extra_context}")
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

        # Validate structured records
        from schemas import known_record_types, validate_record
        from pydantic import ValidationError as _VE
        valid_types = set(known_record_types())
        structured = []
        for sr in result.get("structured_records", []):
            if not isinstance(sr, dict):
                continue
            rtype = str(sr.get("record_type", "")).strip().lower()
            data = sr.get("data")
            if rtype not in valid_types or not isinstance(data, dict):
                continue
            try:
                cleaned = validate_record(rtype, data)
            except _VE:
                # AI emitted something the schema rejects — skip rather than poison the row.
                continue
            structured.append({"record_type": rtype, "data": cleaned})

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
            structured_records=structured,
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
