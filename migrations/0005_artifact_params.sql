-- 产物级参数：一条任务里每张图可以有各自的大小/步数/CFG/种子（"导演"批量规划出来的图就是这种），
-- 所以参数必须跟着**产物**走，而不是只记在任务上——否则单张图没法精确复现或微调。
-- 可空：历史产物与单参数任务都没有这一列的值。
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS params JSONB;
