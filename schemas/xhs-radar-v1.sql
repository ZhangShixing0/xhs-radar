PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_version', '1');

CREATE TABLE IF NOT EXISTS radar_runs (
    id TEXT PRIMARY KEY,
    layer TEXT NOT NULL CHECK(layer IN ('recent', 'topic', 'classic', 'baseline')),
    status TEXT NOT NULL CHECK(status IN ('planned', 'running', 'completed', 'partial', 'failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    config_hash TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_author_id TEXT NOT NULL,
    display_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(platform, platform_author_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_note_id TEXT NOT NULL,
    author_id INTEGER REFERENCES authors(id),
    canonical_url TEXT,
    title TEXT,
    note_type TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    cover_url TEXT,
    content_hash TEXT,
    current_liked_count INTEGER NOT NULL DEFAULT 0 CHECK(current_liked_count >= 0),
    current_collected_count INTEGER NOT NULL DEFAULT 0 CHECK(current_collected_count >= 0),
    current_comment_count INTEGER NOT NULL DEFAULT 0 CHECK(current_comment_count >= 0),
    current_shared_count INTEGER NOT NULL DEFAULT 0 CHECK(current_shared_count >= 0),
    UNIQUE(platform, platform_note_id)
);

CREATE TABLE IF NOT EXISTS search_queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_key TEXT NOT NULL UNIQUE,
    query_text TEXT NOT NULL,
    group_name TEXT NOT NULL CHECK(group_name IN ('fixed', 'seasonal', 'decision', 'exploration', 'legacy')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    priority INTEGER NOT NULL DEFAULT 100,
    source_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES radar_runs(id) ON DELETE CASCADE,
    query_id INTEGER NOT NULL REFERENCES search_queries(id),
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    layer TEXT NOT NULL CHECK(layer IN ('recent', 'topic', 'classic', 'baseline')),
    observed_at TEXT NOT NULL,
    result_rank INTEGER,
    time_filter TEXT,
    sort_mode TEXT,
    UNIQUE(run_id, query_id, note_id)
);

CREATE TABLE IF NOT EXISTS note_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES radar_runs(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    liked_count INTEGER NOT NULL CHECK(liked_count >= 0),
    collected_count INTEGER NOT NULL CHECK(collected_count >= 0),
    comment_count INTEGER NOT NULL CHECK(comment_count >= 0),
    shared_count INTEGER NOT NULL CHECK(shared_count >= 0),
    source TEXT NOT NULL CHECK(source IN ('search', 'detail')),
    UNIQUE(note_id, run_id)
);

CREATE TABLE IF NOT EXISTS note_details (
    note_id INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    description TEXT,
    description_hash TEXT,
    image_count INTEGER NOT NULL DEFAULT 0 CHECK(image_count >= 0),
    returned_comment_count INTEGER NOT NULL DEFAULT 0 CHECK(returned_comment_count >= 0),
    selection_reason TEXT
);

CREATE TABLE IF NOT EXISTS comment_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    captured_at TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    liked_count INTEGER NOT NULL DEFAULT 0 CHECK(liked_count >= 0),
    UNIQUE(note_id, content_hash)
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    owned_advantage_score REAL NOT NULL DEFAULT 0 CHECK(owned_advantage_score BETWEEN 0 AND 100),
    owned_advantage_evidence TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_topic_links (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    assigned_by TEXT NOT NULL CHECK(assigned_by IN ('human_coded', 'rule_v1', 'manual_review')),
    evidence TEXT,
    PRIMARY KEY(note_id, topic_id)
);

CREATE TABLE IF NOT EXISTS topic_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES radar_runs(id) ON DELETE CASCADE,
    evaluated_at TEXT NOT NULL,
    note_count INTEGER NOT NULL,
    distinct_author_count INTEGER NOT NULL,
    above_query_median_count INTEGER NOT NULL,
    detail_note_count INTEGER NOT NULL,
    comment_signal_count INTEGER NOT NULL,
    growth_score REAL,
    save_intent_score REAL,
    cross_author_score REAL,
    comment_demand_score REAL,
    owned_advantage_score REAL,
    opportunity_score REAL NOT NULL,
    score_coverage REAL NOT NULL CHECK(score_coverage BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK(status IN ('single_author', 'weak', 'watch', 'validated')),
    evidence_json TEXT NOT NULL,
    UNIQUE(topic_id, run_id)
);

CREATE INDEX IF NOT EXISTS idx_note_snapshots_note_time ON note_snapshots(note_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_query ON search_observations(query_id, run_id);
CREATE INDEX IF NOT EXISTS idx_topic_links_topic ON note_topic_links(topic_id, note_id);
CREATE INDEX IF NOT EXISTS idx_comments_note ON comment_signals(note_id);

