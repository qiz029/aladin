-- 删除任务后，Modal 结果 Volume 上那份副本的待删清单。
-- api 不持有 Modal 凭据（ADR-0002），只登记；worker 定期执行删除。
CREATE TABLE remote_purge (
    id         BIGSERIAL PRIMARY KEY,
    volume     TEXT NOT NULL,
    path       TEXT NOT NULL CHECK (path ~ '^[0-9a-f]{64}$'),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (volume, path)
);
