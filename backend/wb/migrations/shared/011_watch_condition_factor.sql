-- A fail line is a price of the day it was set: its price factor then (adjusted ÷ exchange close of the latest bar),
-- so a later split or 送转 does not move the close across it. NULL: no bar then — compared on the exchange's price.
ALTER TABLE watch_condition ADD COLUMN factor REAL;
