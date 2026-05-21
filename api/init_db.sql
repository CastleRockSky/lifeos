-- ============================================================================
-- LifeOS — Database Schema (Phase 1)
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ── Subjects (universal entity) ──────────────────────────────────────────
CREATE TABLE subjects (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50) NOT NULL,              -- person, pet, vehicle, property
    profile_data JSONB DEFAULT '{}',        -- flexible per type
    is_primary BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- ── Documents ────────────────────────────────────────────────────────────
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title VARCHAR(500) NOT NULL,
    file_path VARCHAR(1000) NOT NULL,
    original_filename VARCHAR(500),
    file_size_bytes BIGINT,
    mime_type VARCHAR(100),
    file_type VARCHAR(50),                  -- pdf, image, text, email
    domain VARCHAR(50),                     -- medical, financial, auto, home, vet, legal, insurance
    category VARCHAR(100),                  -- lab_result, tax_return, etc.
    subject_id UUID REFERENCES subjects(id),
    source VARCHAR(50) DEFAULT 'upload',    -- upload, email_forward, mobile_capture, api
    content_text TEXT,                      -- extracted/OCR text
    text_length INTEGER DEFAULT 0,
    ocr_applied BOOLEAN DEFAULT false,
    ocr_confidence FLOAT,
    page_count INTEGER,
    file_hash VARCHAR(64),                  -- SHA256 of the file bytes (exact-dup detection)
    embedding_status VARCHAR(50) DEFAULT 'pending', -- pending, complete, failed
    ai_summary TEXT,
    ai_extracted_data JSONB,
    ai_action_items JSONB,
    ai_status TEXT DEFAULT 'pending',       -- pending, analyzing, complete, failed, skipped
    ai_confidence REAL,                     -- 0.0-1.0
    review_status TEXT DEFAULT 'none',      -- none, needs_review, reviewed
    document_date DATE,                     -- date extracted from document content
    expiration_date DATE,                   -- expiration date if applicable
    ai_analyzed_at TIMESTAMPTZ,
    ai_prompt_version INTEGER DEFAULT 1,
    ai_suggestion JSONB,                    -- staged re-analysis metadata awaiting review (NULL when none)
    tags TEXT[] DEFAULT '{}',
    uploaded_by VARCHAR(100),
    email_message_id UUID,                  -- FK added below (forward declaration)
    ingested_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- ── Document Chunks ──────────────────────────────────────────────────────
CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    char_start INTEGER,
    char_end INTEGER,
    embedding_id VARCHAR(100),              -- Qdrant point ID reference
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Structured Records ──────────────────────────────────────────────────
CREATE TABLE structured_records (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain VARCHAR(50),
    record_type VARCHAR(100) NOT NULL,      -- medication, account, vehicle, policy, provider
    subject_id UUID REFERENCES subjects(id),
    data JSONB NOT NULL,                    -- validated per record_type on write
    source_document_id UUID REFERENCES documents(id),
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    next_action_date TIMESTAMPTZ,
    next_action_description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

-- ── Time Series Metrics ─────────────────────────────────────────────────
CREATE TABLE time_series_metrics (
    id BIGSERIAL PRIMARY KEY,
    subject_id UUID NOT NULL REFERENCES subjects(id),
    metric_type VARCHAR(100) NOT NULL,      -- weight, blood_pressure, mileage, etc.
    value_numeric NUMERIC(12,2),
    value_text VARCHAR(100),                -- for non-numeric like "120/80"
    recorded_at TIMESTAMPTZ NOT NULL,
    source VARCHAR(50),                     -- manual, document_extract, agent_api
    source_document_id UUID REFERENCES documents(id),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Action Items ─────────────────────────────────────────────────────────
CREATE TABLE action_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    domain VARCHAR(50),
    subject_id UUID REFERENCES subjects(id),
    title VARCHAR(500) NOT NULL,
    description TEXT,
    due_date DATE,
    source_type VARCHAR(50),                -- ai_extracted, manual, recurring
    source_document_id UUID REFERENCES documents(id),
    source_record_id UUID REFERENCES structured_records(id),
    status VARCHAR(50) DEFAULT 'pending',   -- pending, completed, snoozed, dismissed
    priority VARCHAR(50) DEFAULT 'medium',  -- low, medium, high
    calendar_event_id VARCHAR(500),
    recurrence_rule TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);

-- ── Audit Log ────────────────────────────────────────────────────────────
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    table_name VARCHAR(100),
    record_id UUID,
    action VARCHAR(50) NOT NULL,            -- upload, download, update, delete, search, query
    user_email VARCHAR(200),
    details JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Email Messages (Phase 3) ─────────────────────────────────────────────
-- One row per ingested email. Documents created from attachments (or from
-- the email body itself, when there are no attachments) reference this row
-- via documents.email_message_id.
CREATE TABLE email_messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    message_id VARCHAR(998) UNIQUE,         -- RFC 5322 Message-ID (used for dedup)
    imap_uid BIGINT,                        -- UID assigned by source mailbox
    sender VARCHAR(500),                    -- "from" address
    original_sender VARCHAR(500),           -- parsed from forwarded body if present
    recipient VARCHAR(500),                 -- "to" address (the LifeOS inbox)
    subject TEXT,
    clean_subject TEXT,                     -- subject with Fwd:/Re: stripped
    body_text TEXT,
    body_html TEXT,
    received_at TIMESTAMPTZ,
    attachment_count INTEGER DEFAULT 0,
    document_count INTEGER DEFAULT 0,       -- documents successfully created
    status VARCHAR(50) DEFAULT 'pending',   -- pending, processing, processed, failed, partial
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    raw_size_bytes BIGINT,
    domain_hint VARCHAR(50),                -- from sender map, if matched
    category_hint VARCHAR(100),
    subject_hint VARCHAR(255),
    processed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE documents
    ADD CONSTRAINT documents_email_message_id_fkey
    FOREIGN KEY (email_message_id) REFERENCES email_messages(id) ON DELETE SET NULL;

-- ── Medication Doses (Phase 5) ───────────────────────────────────────────
-- Adherence log: one row per scheduled dose event (taken / missed / late).
CREATE TABLE medication_doses (
    id BIGSERIAL PRIMARY KEY,
    medication_record_id UUID NOT NULL REFERENCES structured_records(id) ON DELETE CASCADE,
    subject_id UUID REFERENCES subjects(id),
    scheduled_at TIMESTAMPTZ,                -- when the dose was due (optional)
    recorded_at TIMESTAMPTZ NOT NULL,        -- when the user/agent reported it
    status VARCHAR(20) NOT NULL,             -- taken, missed, late, skipped
    notes TEXT,
    source VARCHAR(50) DEFAULT 'agent_api',  -- agent_api, manual
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── Email Sender Map (Phase 3) ───────────────────────────────────────────
-- Builds up over time: known senders → domain/category/subject pre-classifier.
CREATE TABLE email_sender_map (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    sender_pattern VARCHAR(500) NOT NULL UNIQUE,  -- exact address or "*@domain.com"
    domain VARCHAR(50),
    category VARCHAR(100),
    subject_hint VARCHAR(255),              -- e.g. pet name for vet emails
    notes TEXT,
    auto_learned BOOLEAN DEFAULT false,     -- true if added by the system, false if manual
    confidence REAL DEFAULT 0.0,            -- raised as more emails confirm the mapping
    match_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_matched_at TIMESTAMPTZ
);

-- ── Agent API Keys ──────────────────────────────────────────────────────
CREATE TABLE agent_api_keys (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    key_hash VARCHAR(255) NOT NULL UNIQUE,
    agent_name VARCHAR(100) NOT NULL,
    allowed_domains TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);

-- Duplicate detection: each row flags `document_id` as a possible duplicate
-- of the earlier `duplicate_of_id`. Flags are advisory — never auto-deleted.
CREATE TABLE duplicate_flags (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id UUID NOT NULL REFERENCES documents(id),
    duplicate_of_id UUID NOT NULL REFERENCES documents(id),
    match_type VARCHAR(20) NOT NULL,        -- 'exact' | 'semantic'
    similarity_score REAL NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | dismissed
    created_at TIMESTAMPTZ DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    UNIQUE (document_id, duplicate_of_id)
);

-- ── Indexes ──────────────────────────────────────────────────────────────

-- Documents
CREATE INDEX idx_documents_domain ON documents(domain);
CREATE INDEX idx_documents_category ON documents(category);
CREATE INDEX idx_documents_subject ON documents(subject_id);
CREATE INDEX idx_documents_ingested ON documents(ingested_at);
CREATE INDEX idx_documents_deleted ON documents(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_documents_text_search ON documents
    USING gin(to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content_text, '')));

CREATE INDEX idx_documents_ai_status ON documents(ai_status);
CREATE INDEX idx_documents_review_status ON documents(review_status) WHERE review_status = 'needs_review';
CREATE INDEX idx_documents_expiration ON documents(expiration_date) WHERE expiration_date IS NOT NULL;
CREATE INDEX idx_documents_file_hash ON documents(file_hash) WHERE file_hash IS NOT NULL;

-- Duplicate flags
CREATE INDEX idx_duplicate_flags_document ON duplicate_flags(document_id);
CREATE INDEX idx_duplicate_flags_dup_of ON duplicate_flags(duplicate_of_id);
CREATE INDEX idx_duplicate_flags_pending ON duplicate_flags(status) WHERE status = 'pending';

-- Document Chunks
CREATE INDEX idx_chunks_document ON document_chunks(document_id);

-- Structured Records
CREATE INDEX idx_records_domain ON structured_records(domain);
CREATE INDEX idx_records_type ON structured_records(record_type);
CREATE INDEX idx_records_subject ON structured_records(subject_id);
CREATE INDEX idx_records_next_action ON structured_records(next_action_date)
    WHERE next_action_date IS NOT NULL;
CREATE INDEX idx_records_data ON structured_records USING gin(data);
CREATE INDEX idx_records_deleted ON structured_records(deleted_at) WHERE deleted_at IS NULL;

-- Time Series Metrics (composite for range queries)
CREATE INDEX idx_metrics_subject_type_date
    ON time_series_metrics(subject_id, metric_type, recorded_at);

-- Action Items
CREATE INDEX idx_actions_status_due ON action_items(status, due_date);
CREATE INDEX idx_actions_domain ON action_items(domain);
CREATE INDEX idx_actions_deleted ON action_items(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_actions_subject ON action_items(subject_id);
CREATE INDEX idx_actions_source_doc ON action_items(source_document_id);

-- Audit Log
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at);

-- Agent API Keys
CREATE INDEX idx_agent_keys_active ON agent_api_keys(is_active) WHERE is_active = true;

-- Email Messages
CREATE INDEX idx_email_messages_status ON email_messages(status);
CREATE INDEX idx_email_messages_received ON email_messages(received_at DESC);
CREATE INDEX idx_email_messages_sender ON email_messages(sender);

-- Documents → Email link
CREATE INDEX idx_documents_email_message ON documents(email_message_id) WHERE email_message_id IS NOT NULL;

-- Email Sender Map
CREATE INDEX idx_email_sender_map_pattern ON email_sender_map(sender_pattern);

-- Medication Doses
CREATE INDEX idx_med_doses_record ON medication_doses(medication_record_id, recorded_at DESC);
CREATE INDEX idx_med_doses_subject ON medication_doses(subject_id, recorded_at DESC);

-- ── Updated_at Trigger ──────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER subjects_updated_at
    BEFORE UPDATE ON subjects FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER documents_updated_at
    BEFORE UPDATE ON documents FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER structured_records_updated_at
    BEFORE UPDATE ON structured_records FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER action_items_updated_at
    BEFORE UPDATE ON action_items FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER email_messages_updated_at
    BEFORE UPDATE ON email_messages FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER email_sender_map_updated_at
    BEFORE UPDATE ON email_sender_map FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Seed Data ───────────────────────────────────────────────────────────

INSERT INTO subjects (name, type, is_primary, profile_data)
VALUES ('Dave', 'person', true, '{"location": "Castle Rock, CO"}');
