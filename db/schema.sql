CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    domain TEXT DEFAULT 'personal',
    due_date DATE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    is_done BOOLEAN DEFAULT 0,
    due_time TEXT,                       -- 'HH:MM', локальное MSK
    reminder_sent BOOLEAN DEFAULT 0,
    recurrence TEXT                      -- как habits.schedule + 'monthly:N'
);

CREATE TABLE IF NOT EXISTS notes_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    chunk_count INTEGER,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    domain TEXT DEFAULT 'personal',
    start_at DATETIME NOT NULL,          -- локальное MSK, 'YYYY-MM-DD HH:MM:SS'
    reminder_sent BOOLEAN DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    domain TEXT DEFAULT 'personal',
    schedule TEXT NOT NULL,              -- 'daily' | 'wd:0,2,4' (Пн=0 .. Вс=6)
    active BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS habit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    habit_id INTEGER NOT NULL,
    done_date DATE NOT NULL,
    UNIQUE(habit_id, done_date)          -- идемпотентность отметки за день
);
