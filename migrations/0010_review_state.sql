-- 一句话出图「先看计划再生成」：规划完停在 review，等人确认（可编辑）后才进入 pending。
ALTER TABLE jobs DROP CONSTRAINT jobs_state_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_state_check CHECK (state IN
    ('pending', 'submitting', 'submitted', 'running', 'succeeded', 'failed', 'unknown', 'review'));
