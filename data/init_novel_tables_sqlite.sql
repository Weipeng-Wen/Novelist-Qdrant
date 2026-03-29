-- SQLite schema for novel agent core tables

CREATE TABLE IF NOT EXISTS character_profiles (
    novel_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    character_name TEXT NOT NULL,
    profile_detail TEXT NOT NULL,
    PRIMARY KEY (novel_id, character_id)
);

CREATE TABLE IF NOT EXISTS chapter_outlines (
    novel_id INTEGER NOT NULL,
    is_completed INTEGER NOT NULL DEFAULT 0 CHECK (is_completed IN (0, 1)),
    novel_intro TEXT NOT NULL DEFAULT '',
    writing_style TEXT NOT NULL,
    title TEXT NOT NULL,
    PRIMARY KEY (novel_id)
);

CREATE TABLE IF NOT EXISTS chapter_summaries (
    novel_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    chapter_title TEXT NOT NULL DEFAULT '',
    chapter_summary TEXT NOT NULL,
    chapter_full_text TEXT NOT NULL,
    word_count INTEGER NOT NULL CHECK (word_count >= 0),
    PRIMARY KEY (novel_id, chapter_id)
);
