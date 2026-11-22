-- How the latest embedding attempt went (one row), so a failure shows in GET / and the UI rather than
-- only in the worker's log. Cleared by the next attempt that succeeds.

CREATE TABLE embedding_status (
    id boolean PRIMARY KEY DEFAULT TRUE CHECK (id),
    last_error text,
    last_error_at timestamptz
);
INSERT INTO embedding_status DEFAULT VALUES;
