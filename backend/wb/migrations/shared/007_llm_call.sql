-- Every LLM call (spec §10.1): which task asked which model, the prompt template's and the inputs' hashes (to
-- replay an answer when it has to be reproduced), what came back, tokens, cost (元) and time. In shared.sqlite:
-- the spend and its budget are yours, not a data profile's. The API key is never stored here.
CREATE TABLE llm_call (
  id            TEXT PRIMARY KEY,
  task          TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  template_hash TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  request       TEXT NOT NULL,
  output        TEXT,
  ok            INTEGER NOT NULL,
  error         TEXT,
  tokens_in     INTEGER,
  tokens_out    INTEGER,
  cost          REAL,
  latency_ms    INTEGER,
  created_at    TEXT NOT NULL
);
CREATE INDEX llm_call_created ON llm_call (created_at);
CREATE INDEX llm_call_replay ON llm_call (task, template_hash, evidence_hash);
