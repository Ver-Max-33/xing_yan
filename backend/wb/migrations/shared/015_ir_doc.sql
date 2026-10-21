-- 你导入的 IR 资料 (spec §10.3 文档片段): a document you add to a stock (a results deck, a briefing, your notes from a
-- call) — its text kept page by page in <data root>/docs/ir/<id>.txt; the excerpt a research gets, and its page, are
-- yours to choose. Yours, so shared by the data profiles and kept until you delete it.
CREATE TABLE ir_doc (
  id         TEXT PRIMARY KEY,
  listing_id TEXT NOT NULL,
  title      TEXT NOT NULL,
  filename   TEXT NOT NULL,
  pages      INTEGER NOT NULL,
  chars      INTEGER NOT NULL,
  excerpt    TEXT NOT NULL,
  page       INTEGER NOT NULL,       -- the excerpt's page (1-based)
  added_at   TEXT NOT NULL
);
CREATE INDEX ir_doc_by_listing ON ir_doc (listing_id, added_at);
