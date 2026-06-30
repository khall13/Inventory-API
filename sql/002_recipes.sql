-- Recipes — primary domain (plan §5). Natural key = recipe `number`.
-- raw landing first, then the typed ct.* tables the transform populates.

-- ── Raw landing ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw.recipes (
    natural_key      TEXT PRIMARY KEY,           -- recipeEnhancedHeaderDetails.number
    payload          JSONB NOT NULL,             -- full recipeEnhancedDetails[] element
    content_hash     TEXT NOT NULL,              -- md5 of payload → change detection
    label            TEXT,                       -- recipe name (for the daily email)
    source_endpoint  TEXT NOT NULL,
    page             INTEGER,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    seen_run_at      TIMESTAMPTZ
);

-- ── Typed warehouse: parent ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ct.recipes (
    number                       TEXT PRIMARY KEY,
    name                         TEXT,
    active_flag                  TEXT,           -- Y/N
    category_name                TEXT,
    subcategory_name             TEXT,
    microcategory_name           TEXT,
    plu_number                   TEXT,
    universal_product_code       TEXT,
    price                        NUMERIC,
    batch_quantity               NUMERIC,
    batch_package_type           TEXT,
    portion_amount               NUMERIC,
    portion_yield                NUMERIC,
    inventory_unit_package_type  TEXT,
    prep_station_name            TEXT,
    effective_date               DATE,
    expiration_date              DATE,
    shelf_life_duration          NUMERIC,
    shelf_life_type              TEXT,           -- D/H/M
    prep_time_duration           NUMERIC,
    prep_time_type               TEXT,
    recipe_class                 TEXT,           -- S/I
    production_type              TEXT,           -- P/O
    recipe_status                TEXT,
    recipe_pos_decrement         TEXT,
    minimum_batch                NUMERIC,
    maximum_batch                NUMERIC,
    nutrition_id                 TEXT,
    preparation_notes            TEXT,
    plating_instructions         TEXT,
    last_touch_date              DATE,
    -- recipe_unit_one..five and issue_unit_one/two package_type/yield pairs are
    -- preserved in the raw payload; promote to columns once confirmed in the sample.
    units                        JSONB,          -- interim home for the unit/yield pairs
    transformed_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── Typed warehouse: children (each FK → ct.recipes.number) ───────────────────
CREATE TABLE IF NOT EXISTS ct.recipe_components (
    recipe_number        TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    sequence             INTEGER,
    number               TEXT,                   -- ingredient product # (matches a recipe# ⇒ sub-recipe)
    name                 TEXT,
    quantity             NUMERIC,
    recipe_package       TEXT,
    scaling_factor       NUMERIC,
    major_ingredient     TEXT,
    pre_production       TEXT,
    special_instruction1 TEXT,
    special_instruction2 TEXT,
    PRIMARY KEY (recipe_number, sequence, number)
);
CREATE INDEX IF NOT EXISTS ix_recipe_components_number ON ct.recipe_components(number);

CREATE TABLE IF NOT EXISTS ct.recipe_component_substitutes (
    recipe_number     TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    component_number  TEXT,
    name              TEXT,
    number            TEXT,
    quantity          NUMERIC,
    recipe_package    TEXT,
    scaling_factor    NUMERIC
);

CREATE TABLE IF NOT EXISTS ct.recipe_upcs (
    recipe_number          TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    universal_product_code TEXT,
    active_flag            TEXT
);

CREATE TABLE IF NOT EXISTS ct.recipe_ship_locations (
    recipe_number   TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    location_number TEXT,
    active_flag     TEXT
);

CREATE TABLE IF NOT EXISTS ct.recipe_allergens (
    recipe_number    TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    nutrition_class  TEXT,
    nutrition_value  TEXT,
    active_flag      TEXT
);

CREATE TABLE IF NOT EXISTS ct.recipe_concepts (
    recipe_number  TEXT NOT NULL REFERENCES ct.recipes(number) ON DELETE CASCADE,
    concept_name   TEXT,
    active_flag    TEXT
);
