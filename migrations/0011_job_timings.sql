-- 生成耗时：容器内分段（result.json 的 timings）+ 宿主侧的发现完成时刻与下载耗时。
-- 其余时刻（提交、容器开始/结束、规划完成）在 job_events 里，报表两边一起看。
ALTER TABLE jobs ADD COLUMN timings JSONB;
