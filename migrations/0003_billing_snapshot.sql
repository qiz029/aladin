-- Modal 账单快照：worker 周期性拉 Workspace.billing.summary 写这里，api 只读。
-- api 容器不持有 Modal 凭据（ADR-0002），账单遵守同一条边界。
-- 只保留最新一行——这是「花到哪了」的展示，不是账单归档；明细用 modal billing report。

CREATE TABLE billing_snapshot (
    id              SMALLINT PRIMARY KEY CHECK (id = 1),  -- 单行表
    cycle_start     TIMESTAMPTZ NOT NULL,
    cycle_end       TIMESTAMPTZ NOT NULL,
    metered_cost    NUMERIC NOT NULL,
    billed_cost     NUMERIC NOT NULL,
    credits_applied NUMERIC NOT NULL,
    credit_grant    NUMERIC,   -- 本期额度（settings.CREDIT_GRANT）；NULL = 未配置，不算余额
    adjustments     JSONB NOT NULL,
    breakdown       JSONB NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
