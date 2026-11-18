-- Dashboard docs/invoicing: the accounting CRUD suite the MeshyDocs app drives
-- (clients, invoices + line items, documents, expenses, payouts). All
-- owner-scoped; no RLS (read on the pool, every query is owner-id-scoped in
-- application code, mirroring the rest of the dashboard data layer). Idempotent
-- CREATE TABLE IF NOT EXISTS.
--
-- NOTE: depends on migration 089 (dashboard password/MFA) only for ordering —
-- merge that PR first so the version sequence stays gap-free in production.

CREATE TABLE IF NOT EXISTS dashboard_clients (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id    UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    company     TEXT,
    email       TEXT,
    address     TEXT,
    city        TEXT,
    state       TEXT,
    country     TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS dashboard_clients_owner_idx ON dashboard_clients (owner_id);

CREATE TABLE IF NOT EXISTS dashboard_invoices (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id            UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    number              TEXT NOT NULL,
    client_id           UUID REFERENCES dashboard_clients(id) ON DELETE SET NULL,
    status              TEXT NOT NULL DEFAULT 'draft',
    issue_date          DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date            DATE NOT NULL DEFAULT CURRENT_DATE,
    subtotal_cents      BIGINT NOT NULL DEFAULT 0,
    tax_rate            TEXT NOT NULL DEFAULT '0',
    tax_cents           BIGINT NOT NULL DEFAULT 0,
    total_cents         BIGINT NOT NULL DEFAULT 0,
    notes               TEXT,
    stripe_payment_link TEXT,
    paid_at             TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, number)
);
CREATE INDEX IF NOT EXISTS dashboard_invoices_owner_idx ON dashboard_invoices (owner_id);

CREATE TABLE IF NOT EXISTS dashboard_invoice_items (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id       UUID NOT NULL REFERENCES dashboard_invoices(id) ON DELETE CASCADE,
    description      TEXT NOT NULL DEFAULT '',
    quantity         TEXT NOT NULL DEFAULT '1',
    unit_price_cents BIGINT NOT NULL DEFAULT 0,
    amount_cents     BIGINT NOT NULL DEFAULT 0,
    sort_order       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS dashboard_invoice_items_invoice_idx ON dashboard_invoice_items (invoice_id);

CREATE TABLE IF NOT EXISTS dashboard_documents (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id         UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    number           TEXT NOT NULL,
    type             TEXT NOT NULL DEFAULT 'document',
    client_id        UUID REFERENCES dashboard_clients(id) ON DELETE SET NULL,
    status           TEXT NOT NULL DEFAULT 'draft',
    template_version TEXT NOT NULL DEFAULT '1',
    fields           JSONB NOT NULL DEFAULT '{}'::jsonb,
    signed_at        TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (owner_id, number)
);
CREATE INDEX IF NOT EXISTS dashboard_documents_owner_idx ON dashboard_documents (owner_id);

CREATE TABLE IF NOT EXISTS dashboard_expenses (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id      UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    date          DATE NOT NULL DEFAULT CURRENT_DATE,
    vendor        TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'general',
    amount_cents  BIGINT NOT NULL DEFAULT 0,
    currency      TEXT NOT NULL DEFAULT 'USD',
    client_id     UUID REFERENCES dashboard_clients(id) ON DELETE SET NULL,
    invoice_id    UUID REFERENCES dashboard_invoices(id) ON DELETE SET NULL,
    is_billable   BOOLEAN NOT NULL DEFAULT FALSE,
    is_deductible BOOLEAN NOT NULL DEFAULT TRUE,
    receipt_url   TEXT,
    notes         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS dashboard_expenses_owner_idx ON dashboard_expenses (owner_id);

CREATE TABLE IF NOT EXISTS dashboard_payouts (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id                        UUID NOT NULL REFERENCES dashboard_users(id) ON DELETE CASCADE,
    contractor                      TEXT NOT NULL DEFAULT '',
    period_start                    DATE NOT NULL DEFAULT CURRENT_DATE,
    period_end                      DATE NOT NULL DEFAULT CURRENT_DATE,
    status                          TEXT NOT NULL DEFAULT 'pending',
    americas_gross_cents            BIGINT NOT NULL DEFAULT 0,
    americas_deductions_cents       BIGINT NOT NULL DEFAULT 0,
    americas_net_cents              BIGINT NOT NULL DEFAULT 0,
    americas_platform_share_cents      BIGINT NOT NULL DEFAULT 0,
    americas_contractor_share_cents BIGINT NOT NULL DEFAULT 0,
    europe_gross_cents              BIGINT NOT NULL DEFAULT 0,
    europe_deductions_cents         BIGINT NOT NULL DEFAULT 0,
    europe_net_cents                BIGINT NOT NULL DEFAULT 0,
    europe_platform_share_cents        BIGINT NOT NULL DEFAULT 0,
    europe_contractor_share_cents   BIGINT NOT NULL DEFAULT 0,
    asia_gross_cents                BIGINT NOT NULL DEFAULT 0,
    asia_deductions_cents           BIGINT NOT NULL DEFAULT 0,
    asia_net_cents                  BIGINT NOT NULL DEFAULT 0,
    asia_platform_share_cents          BIGINT NOT NULL DEFAULT 0,
    asia_contractor_share_cents     BIGINT NOT NULL DEFAULT 0,
    total_owed_cents                BIGINT NOT NULL DEFAULT 0,
    paid_at                         TIMESTAMPTZ,
    payment_method                  TEXT,
    notes                           TEXT,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS dashboard_payouts_owner_idx ON dashboard_payouts (owner_id);
