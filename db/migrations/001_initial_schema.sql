DROP SCHEMA IF EXISTS public CASCADE;
CREATE SCHEMA public;

CREATE TABLE promotions (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    slug TEXT UNIQUE NOT NULL
);

CREATE TABLE fighters (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    nickname TEXT,
    headshot_url TEXT,
    nationality TEXT,
    birth_date DATE,
    height_cm NUMERIC,
    reach_cm NUMERIC,
    stance TEXT,
    weight_grams INTEGER,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    draws INTEGER DEFAULT 0,
    source TEXT,
    source_id TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, source_id)
);

CREATE INDEX idx_fighters_name ON fighters(name);
CREATE INDEX idx_fighters_birth_date ON fighters(birth_date);

CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    event_date DATE,
    location TEXT,
    promotion_id INTEGER REFERENCES promotions(id)
);

CREATE INDEX idx_events_event_date ON events(event_date);
CREATE INDEX idx_events_promotion_id ON events(promotion_id);

CREATE TABLE fights (
    id SERIAL PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    fighter_red_id INTEGER REFERENCES fighters(id),
    fighter_blue_id INTEGER REFERENCES fighters(id),
    weight_class TEXT,
    weight_grams INTEGER,
    scheduled_rounds INTEGER,
    winner_id INTEGER REFERENCES fighters(id),
    method TEXT,
    end_round INTEGER,
    end_time TEXT,
    odds_red NUMERIC,
    odds_blue NUMERIC,
    source TEXT,
    source_id TEXT,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, source_id)
);

CREATE INDEX idx_fights_event_id ON fights(event_id);
CREATE INDEX idx_fights_fighter_red_id ON fights(fighter_red_id);
CREATE INDEX idx_fights_fighter_blue_id ON fights(fighter_blue_id);

CREATE TABLE fight_stats (
    id SERIAL PRIMARY KEY,
    fight_id INTEGER REFERENCES fights(id),
    fighter_id INTEGER REFERENCES fighters(id),
    sig_strikes_landed INTEGER,
    sig_strikes_attempted INTEGER,
    takedowns_landed INTEGER,
    takedowns_attempted INTEGER,
    submission_attempts INTEGER,
    control_time_seconds INTEGER,
    knockdowns INTEGER,
    UNIQUE (fight_id, fighter_id)
);

CREATE TABLE rankings (
    id SERIAL PRIMARY KEY,
    fighter_id INTEGER REFERENCES fighters(id),
    promotion_id INTEGER REFERENCES promotions(id),
    division TEXT NOT NULL,
    rank_position INTEGER NOT NULL,
    snapshot_date DATE NOT NULL,
    UNIQUE (fighter_id, promotion_id, division, snapshot_date)
);

CREATE INDEX idx_rankings_promotion_division_snapshot_date ON rankings(promotion_id, division, snapshot_date);

CREATE TABLE news (
    id SERIAL PRIMARY KEY,
    headline TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    url TEXT,
    published_at TIMESTAMPTZ,
    fighter_id INTEGER REFERENCES fighters(id),
    category TEXT,
    relevance INTEGER
);

CREATE INDEX idx_news_fighter_id ON news(fighter_id);
CREATE INDEX idx_news_published_at ON news(published_at);

INSERT INTO promotions (name, slug)
VALUES
    ('UFC', 'ufc'),
    ('ONE Championship', 'one-championship'),
    ('PFL', 'pfl');