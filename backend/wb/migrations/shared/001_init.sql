-- Shared by both profiles (spec §4.3). Rules, labels, notes etc. arrive with later phases.
CREATE TABLE setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
