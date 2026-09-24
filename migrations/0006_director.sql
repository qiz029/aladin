-- Director 在同一行任务中先规划、再生图，输入是文字，不要求上传图片。
ALTER TABLE jobs DROP CONSTRAINT jobs_mode_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_mode_check
    CHECK (mode IN ('txt2img', 'img2img', 'edit', 'i2v', 'director'));
ALTER TABLE jobs DROP CONSTRAINT jobs_input_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_input_check
    CHECK (mode IN ('txt2img', 'director') OR
           (input_sha256 IS NOT NULL AND input_path IS NOT NULL));
