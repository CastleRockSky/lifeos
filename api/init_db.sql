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
    embedding_status VARCHAR(50) DEFAULT 'pending', -- pending, complete, failed
    ai_summary TEXT,
    ai_extracted_data JSONB,
    ai_action_items JSONB,
    tags TEXT[] DEFAULT '{}',
    uploaded_by VARCHAR(100),
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

-- ── Indexes ──────────────────────────────────────────────────────────────

-- Documents
CREATE INDEX idx_documents_domain ON documents(domain);
CREATE INDEX idx_documents_category ON documents(category);
CREATE INDEX idx_documents_subject ON documents(subject_id);
CREATE INDEX idx_documents_ingested ON documents(ingested_at);
CREATE INDEX idx_documents_deleted ON documents(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX idx_documents_text_search ON documents
    USING gin(to_tsvector('english', COALESCE(title, '') || ' ' || COALESCE(content_text, '')));

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

-- Audit Log
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at);

-- Agent API Keys
CREATE INDEX idx_agent_keys_active ON agent_api_keys(is_active) WHERE is_active = true;

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

-- ── Seed Data ───────────────────────────────────────────────────────────

INSERT INTO subjects (name, type, is_primary, profile_data)
VALUES ('Dave', 'person', true, '{"location": "Castle Rock, CO"}');
