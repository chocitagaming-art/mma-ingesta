-- 004_events_meta.sql
-- Event metadata for the /eventos feature (upcoming + past cards) and fight-card
-- ordering. The ~3,000 existing events are completed and keep status='completed'
-- via the DEFAULT (untouched). Upcoming events scraped from ufc.com set the new
-- metadata + status='upcoming'.

-- events: status + card metadata + source for idempotent upsert.
ALTER TABLE events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'; -- 'upcoming' | 'completed'
ALTER TABLE events ADD COLUMN IF NOT EXISTS start_time TIMESTAMPTZ;   -- main card start (date+time+tz); event_date stays the date
ALTER TABLE events ADD COLUMN IF NOT EXISTS image_url TEXT;           -- poster / hero image
ALTER TABLE events ADD COLUMN IF NOT EXISTS tagline TEXT;             -- short description
ALTER TABLE events ADD COLUMN IF NOT EXISTS broadcast TEXT;           -- how to watch (ESPN+, Fight Pass, PPV...)
ALTER TABLE events ADD COLUMN IF NOT EXISTS ticket_url TEXT;          -- tickets link
ALTER TABLE events ADD COLUMN IF NOT EXISTS headliner TEXT;           -- "Kape vs Horiguchi"
ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT;              -- 'ufc.com'
ALTER TABLE events ADD COLUMN IF NOT EXISTS source_id TEXT;           -- ufc.com event slug (for upsert)
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_source ON events(source, source_id) WHERE source_id IS NOT NULL;

-- fights: card ordering + name fallback for bouts whose fighters may not be in `fighters`.
ALTER TABLE fights ADD COLUMN IF NOT EXISTS bout_order INTEGER;       -- 1 = main event, ascending
ALTER TABLE fights ADD COLUMN IF NOT EXISTS card_segment TEXT;        -- 'main' | 'prelims' | 'early_prelims'
ALTER TABLE fights ADD COLUMN IF NOT EXISTS fighter_red_name TEXT;    -- scraped name, ALWAYS filled (like rankings.fighter_name)
ALTER TABLE fights ADD COLUMN IF NOT EXISTS fighter_blue_name TEXT;
