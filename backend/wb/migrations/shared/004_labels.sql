-- Box labels (spec §5.10): your own drawing of a box, or of something that is not one, on a chart cut
-- at a date. Prices are exchange prices on their own days (like drawings), so a profile switch or a
-- later ex-date never moves them; the group (tune / test) is set once and never changes.
CREATE TABLE box_label (
    id          TEXT PRIMARY KEY,
    listing_id  TEXT NOT NULL,
    cutoff      TEXT NOT NULL,               -- the last bar you could see
    is_box      INTEGER NOT NULL,            -- 1 是箱体, 0 不是箱体
    category    TEXT NOT NULL,
    start       TEXT NOT NULL,
    end         TEXT NOT NULL,
    top         REAL NOT NULL,               -- raw price on `start`
    bottom      REAL NOT NULL,
    scale       TEXT,                        -- short / mid / long
    context     TEXT,                        -- 上横 / 下横
    position    TEXT,                        -- 低位 / 中继 / 高位
    touches     TEXT NOT NULL DEFAULT '[]',  -- [{date, side, price}]
    exclusions  TEXT NOT NULL DEFAULT '[]',  -- [{date, kind, price, reason}]
    grp         TEXT NOT NULL,               -- tune / test
    redraw_of   TEXT REFERENCES box_label(id) ON DELETE CASCADE,
    verdict     TEXT,                        -- algo_wrong / me_wrong / both_ok, after review
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX box_label_listing ON box_label(listing_id);
