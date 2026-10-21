-- Rules and their versions (spec §5.2, §5.9): a stored version never changes; an edit with
-- different content becomes the next version. Built-in rules ship with the program, not here.
CREATE TABLE rule_version (
  rule_id    TEXT NOT NULL,
  version    INTEGER NOT NULL,
  hash       TEXT NOT NULL,           -- content hash of the checked rule (version field excluded)
  body       TEXT NOT NULL,           -- the YAML / JSON exactly as written
  created_at TEXT NOT NULL,
  PRIMARY KEY (rule_id, version)
);
