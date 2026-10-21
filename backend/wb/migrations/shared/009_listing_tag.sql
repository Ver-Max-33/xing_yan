-- 题材 tags you put on a listing (spec §7.3, until concept boards come in P7): rules read them (TAGGED, universe.tags)
-- and the result lists show them.
CREATE TABLE listing_tag (
  listing_id TEXT NOT NULL,
  tag        TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (listing_id, tag)
);
CREATE INDEX listing_tag_by_tag ON listing_tag (tag);
