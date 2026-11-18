-- Extend skills.mode CHECK to allow the 'lesson' variant introduced
-- alongside the LessonPayload type in meshy-types.

ALTER TABLE skills
    DROP CONSTRAINT skills_mode_check;

ALTER TABLE skills
    ADD CONSTRAINT skills_mode_check
    CHECK (mode IN ('sync', 'async', 'stream', 'lesson'));
