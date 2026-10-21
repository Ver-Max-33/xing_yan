-- Your verdict on a rule's hit in the sample gallery (spec §6.3 Y/N, §6.5 标注即回归测试): kept per
-- rule id (its version lineage), so a later edit shows which of your right hits it loses and which
-- wrong ones it adds. The version and hash say what you were looking at.
CREATE TABLE hit_label (
    rule_id      TEXT NOT NULL,
    listing_id   TEXT NOT NULL,
    signal_date  TEXT NOT NULL,
    verdict      TEXT NOT NULL CHECK (verdict IN ('right', 'wrong')),
    rule_version INTEGER,
    rule_hash    TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (rule_id, listing_id, signal_date)
);
