-- 图生图 / 指令编辑需要输入图。
-- 图片字节不进请求 JSON（否则每行 jobs 会挂上十几 MB），只存路径与哈希：
-- 哈希参与幂等键，路径供 worker 取字节随请求发给容器。

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mode TEXT NOT NULL DEFAULT 'txt2img';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_sha256 TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS input_path TEXT;

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_mode_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_mode_check
    CHECK (mode IN ('txt2img', 'img2img', 'edit'));

-- 非文生图必须有输入，否则任务根本跑不起来，宁可在写库时就拒绝
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_input_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_input_check
    CHECK (mode = 'txt2img' OR (input_sha256 IS NOT NULL AND input_path IS NOT NULL));
