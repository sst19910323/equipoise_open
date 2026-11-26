-- Install before enabling sector-group publication. Existing histories stay in debates.
BEGIN;
CREATE TABLE IF NOT EXISTS debate_group_sessions (
    symbol text NOT NULL,
    as_of timestamptz NOT NULL,
    report jsonb NOT NULL,
    PRIMARY KEY (symbol, as_of)
);
ALTER TABLE debates ADD COLUMN IF NOT EXISTS group_symbol text;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='debates'::regclass
                   AND conname='debates_group_session_fk') THEN
        ALTER TABLE debates ADD CONSTRAINT debates_group_session_fk
            FOREIGN KEY (group_symbol, as_of) REFERENCES debate_group_sessions(symbol, as_of);
        ALTER TABLE debates ADD CONSTRAINT debates_group_ref_matches CHECK (
            group_symbol IS NOT DISTINCT FROM (report->'debate_ref'->>'symbol')
            AND (group_symbol IS NULL OR (
                symbol IS NOT DISTINCT FROM (report->>'symbol')
                AND as_of IS NOT DISTINCT FROM ((report->'debate_ref'->>'as_of')::timestamptz)
            ))
        );
    END IF;
END $$;
CREATE OR REPLACE FUNCTION reject_group_archive_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Published group archives are immutable';
END $$;
CREATE OR REPLACE TRIGGER frozen_group_archive BEFORE UPDATE OR DELETE ON debate_group_sessions
    FOR EACH ROW EXECUTE FUNCTION reject_group_archive_change();
CREATE OR REPLACE FUNCTION protect_group_decision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - 'report_i18n') IS DISTINCT FROM (to_jsonb(OLD) - 'report_i18n') THEN
        RAISE EXCEPTION 'Published group decisions are immutable except for member translations';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER frozen_group_decision BEFORE UPDATE OR DELETE ON debates
    FOR EACH ROW WHEN (OLD.group_symbol IS NOT NULL) EXECUTE FUNCTION protect_group_decision();
COMMIT;
