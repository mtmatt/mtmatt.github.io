-- One row per reader who chose to add their ending. path is a JSON list of the
-- chapters they passed, or NULL if they chose not to send it.
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY,
  work TEXT NOT NULL,
  ending TEXT NOT NULL,
  path TEXT,
  day TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS readings_by_work ON readings (work, ending);
