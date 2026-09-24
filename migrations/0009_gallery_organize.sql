-- 收藏的整理：标签、星级，以及来源模型 / 模式 / 尺度（按它们筛选）。
-- 来源信息在收藏时从任务上抄过来：任务可以被删，收藏不能因此丢了归类。
ALTER TABLE gallery
    ADD COLUMN tags   TEXT[]   NOT NULL DEFAULT '{}',
    ADD COLUMN stars  SMALLINT NOT NULL DEFAULT 0 CHECK (stars BETWEEN 0 AND 5),
    ADD COLUMN model  TEXT,
    ADD COLUMN mode   TEXT,
    ADD COLUMN rating TEXT;

-- 老收藏：从还在的任务上回填
UPDATE gallery g SET
    mode = j.mode,
    rating = j.params->>'rating',
    model = CASE
        WHEN j.app = 'video' THEN '10eros-max'
        WHEN j.mode = 'edit' THEN 'qwen-image-edit-2509'
        ELSE COALESCE(j.params->>'model', 'qwen-image-2.1')
    END
FROM jobs j WHERE j.id = g.source_job;

CREATE INDEX gallery_tags ON gallery USING GIN (tags);
