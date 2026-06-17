ALTER TABLE news
    ADD COLUMN IF NOT EXISTS headline TEXT,
    ADD COLUMN IF NOT EXISTS summary TEXT,
    ADD COLUMN IF NOT EXISTS url TEXT,
    ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS source TEXT,
    ADD COLUMN IF NOT EXISTS fighter_id INTEGER REFERENCES fighters(id),
    ADD COLUMN IF NOT EXISTS category TEXT,
    ADD COLUMN IF NOT EXISTS relevance INTEGER;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'news' AND column_name = 'title'
    ) THEN
        EXECUTE 'UPDATE news SET headline = COALESCE(headline, title)';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'news_url_unique'
    ) THEN
        ALTER TABLE news
            ADD CONSTRAINT news_url_unique UNIQUE (url);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_news_url ON news(url);
CREATE INDEX IF NOT EXISTS idx_news_category ON news(category);