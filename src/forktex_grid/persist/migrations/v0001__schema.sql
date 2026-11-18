-- Grid's tables, in the "{schema}" Postgres schema.
--
-- `apply_migrations` records this file's sha256 and refuses to start if it
-- changes after a database ran it; `tests/test_migration_immutability.py` pins
-- it. A schema change is the next vNNNN file.

CREATE TABLE "{schema}".grid_table (
    id          uuid PRIMARY KEY,
    namespace   varchar(255) NOT NULL,
    slug        varchar(63) NOT NULL,
    label       varchar(255) NOT NULL,
    extends     varchar(255),
    indexes     jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    archived_at timestamptz
);
CREATE UNIQUE INDEX uq_grid_table_slug ON "{schema}".grid_table (namespace, slug) WHERE archived_at IS NULL;

CREATE TABLE "{schema}".grid_column (
    id            uuid PRIMARY KEY,
    table_id      uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    key           varchar(63) NOT NULL,
    label         varchar(255) NOT NULL,
    type_id       varchar(64) NOT NULL,
    cardinality   varchar(8) NOT NULL,
    is_required   boolean NOT NULL,
    is_unique     boolean NOT NULL,
    config        jsonb NOT NULL DEFAULT '{}'::jsonb,
    default_value jsonb,
    display_order integer NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    archived_at   timestamptz,
    CONSTRAINT ck_grid_column_cardinality CHECK (cardinality IN ('one', 'many'))
);
CREATE UNIQUE INDEX uq_grid_column_key ON "{schema}".grid_column (table_id, key) WHERE archived_at IS NULL;

CREATE TABLE "{schema}".grid_row (
    id           uuid PRIMARY KEY,
    table_id     uuid NOT NULL REFERENCES "{schema}".grid_table (id) ON DELETE CASCADE,
    external_ref text,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    archived_at  timestamptz
);
CREATE INDEX ix_grid_row_table ON "{schema}".grid_row (table_id) WHERE archived_at IS NULL;
CREATE INDEX ix_grid_row_payload ON "{schema}".grid_row USING gin (payload jsonb_path_ops);
CREATE UNIQUE INDEX uq_grid_row_external_ref ON "{schema}".grid_row (table_id, external_ref)
    WHERE external_ref IS NOT NULL AND archived_at IS NULL;
