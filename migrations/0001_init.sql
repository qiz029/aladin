-- aladin 初始 schema。
-- 时间统一 TIMESTAMPTZ；参数用 JSONB；job id 保持 TEXT（uuid4().hex），
-- 避免 psycopg 返回 uuid.UUID 后在 web 层出现切片/比较的类型意外。

CREATE TABLE jobs (
    id               TEXT PRIMARY KEY,
    app              TEXT NOT NULL,
    state            TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    params           JSONB NOT NULL,
    request          JSONB NOT NULL,
    result_key       TEXT NOT NULL UNIQUE,
    call_id          TEXT,
    image_count      INTEGER NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 2,
    lease_owner      TEXT,
    lease_expires_at TIMESTAMPTZ,
    next_poll_at     TIMESTAMPTZ,
    last_error       TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at     TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    CONSTRAINT jobs_state_check CHECK (state IN
        ('pending','submitting','submitted','running','succeeded','failed','unknown')),
    CONSTRAINT jobs_result_key_check CHECK (result_key ~ '^[a-f0-9]{64}$')
);

-- 认领查询（提交 / 轮询 / 收尾）走这个索引
CREATE INDEX jobs_claimable ON jobs (state, next_poll_at);
CREATE INDEX jobs_created ON jobs (created_at DESC);

CREATE TABLE job_events (
    id         BIGSERIAL PRIMARY KEY,
    job_id     TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    -- 重开日志流可能重放旧行，靠这个键让重放无害
    dedupe_key TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX job_events_dedupe ON job_events (job_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX job_events_job ON job_events (job_id, id);

CREATE TABLE artifacts (
    id       BIGSERIAL PRIMARY KEY,
    job_id   TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    name     TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    sha256   TEXT NOT NULL,
    bytes    BIGINT NOT NULL,
    seed     BIGINT NOT NULL,
    prompt   TEXT NOT NULL,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, name)
);

CREATE TABLE gallery (
    id         BIGSERIAL PRIMARY KEY,
    rel_path   TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    seed       BIGINT NOT NULL,
    sha256     TEXT NOT NULL UNIQUE,
    bytes      BIGINT NOT NULL,
    source_job TEXT,
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX gallery_at ON gallery (at DESC);
