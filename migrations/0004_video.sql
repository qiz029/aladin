-- 视频切片接入产品链路：mode 放开 i2v（图生视频）。
-- artifacts 表不用改：产物类型靠文件扩展名（.webm）区分，账本字段完全通用；
-- 非文生图必须有输入图这条约束（jobs_input_check）对 i2v 同样成立——它本来就要起始图。
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_mode_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_mode_check
    CHECK (mode IN ('txt2img', 'img2img', 'edit', 'i2v'));
