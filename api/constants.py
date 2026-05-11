"""
constants.py — Domain and category definitions.
"""

DOMAINS = ["medical", "financial", "auto", "home", "vet", "legal", "insurance"]

CATEGORIES = {
    "medical": [
        "lab_result", "visit_note", "prescription", "referral", "imaging_report",
        "surgical_report", "discharge_summary", "vaccination_record", "insurance_eob",
        "insurance_claim", "dental_record", "vision_record", "therapy_note",
        "medical_bill", "prior_authorization", "health_summary", "advance_directive",
    ],
    "financial": [
        "tax_return", "w2", "1099", "bank_statement", "credit_card_statement",
        "loan_agreement", "mortgage_statement", "investment_statement", "receipt",
        "invoice", "pay_stub", "financial_plan", "budget", "credit_report", "tax_estimate",
    ],
    "auto": [
        "registration", "title", "insurance_card", "service_receipt", "recall_notice",
        "purchase_agreement", "lease_agreement", "inspection_report", "warranty", "owners_manual",
    ],
    "home": [
        "mortgage_agreement", "lease", "hoa_document", "insurance_policy", "warranty",
        "contractor_invoice", "permit", "inspection_report", "appraisal",
        "property_tax", "utility_bill", "home_improvement_receipt",
    ],
    "vet": [
        "vet_visit_note", "vaccination_record", "prescription", "lab_result",
        "surgical_report", "boarding_record", "pet_insurance_claim", "adoption_paper",
        "registration", "microchip_record", "dental_record",
    ],
    "legal": [
        "passport", "drivers_license", "birth_certificate", "marriage_certificate",
        "social_security_card", "will", "trust_document", "power_of_attorney",
        "court_document", "contract", "notarized_document",
    ],
    "insurance": [
        "policy_declaration", "premium_notice", "claim", "eob", "coverage_summary",
        "renewal_notice", "cancellation_notice", "agent_correspondence",
    ],
}

ALL_CATEGORIES = [cat for cats in CATEGORIES.values() for cat in cats]

DOMAIN_CATEGORIES = CATEGORIES  # alias for clarity
