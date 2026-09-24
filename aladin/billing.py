"""Modal 账单快照：本期开销、credits 抵扣与余额估算。

账单 API（Workspace.billing.summary）要凭据，而凭据只在 worker 手里
（ADR-0002：api 不持有 Modal 凭据）。所以 worker 周期性拉一次写进
billing_snapshot 表，api 与页面只读这张表——展示不依赖 Modal 可达。

「余额」Modal 没有官方 API（proto 里只有 summary/report/rates），
只能估算：settings.CREDIT_GRANT（本期额度，美元）减本期已抵扣的
credits。不配额度就不显示余额——数字要能对上 Modal 后台。
"""
from __future__ import annotations

from typing import Any

import modal

from . import db, settings


def _snake(key: str) -> str:
    return key.lower().replace(' ', '_')


def _money(value: Any) -> float:
    return float(value or 0)


def summarize(summary: Any) -> dict:
    """把 WorkspaceBillingSummary 折成可入库的字典（Decimal → float）。"""
    adjustments = {_snake(k): _money(v)
                   for k, v in summary.adjustments.items()}
    breakdown = {_snake(k): _money(v)
                 for k, v in summary.metered_cost_breakdown.items()}
    return {
        'cycle_start': summary.start,
        'cycle_end': summary.end,
        'metered_cost': _money(summary.metered_cost),
        'billed_cost': _money(summary.billed_cost),
        'credits_applied': abs(adjustments.get('credits', 0.0)),
        'credit_grant': settings.CREDIT_GRANT,
        'adjustments': adjustments,
        'breakdown': breakdown,
    }


def fetch() -> dict:
    """拉当前计费周期的账单摘要。只该在 worker 进程里调用。"""
    return summarize(modal.Workspace.from_context().billing.summary())


def refresh() -> dict:
    """拉一次并落库，返回这份快照。"""
    payload = fetch()
    db.save_billing(payload)
    return payload


def view() -> dict | None:
    """读快照并补上余额估算。api 与页面共用这一个函数。"""
    row = db.billing_snapshot()
    if row is None:
        return None
    grant = row['credit_grant']
    credits = float(row['credits_applied'])
    return {
        'cycle_start': row['cycle_start'],
        'cycle_end': row['cycle_end'],
        'metered_cost': float(row['metered_cost']),
        'billed_cost': float(row['billed_cost']),
        'credits_applied': credits,
        'credit_grant': float(grant) if grant is not None else None,
        'balance_estimate': ((float(grant) - credits)
                             if grant is not None else None),
        'adjustments': row['adjustments'],
        'breakdown': row['breakdown'],
        'fetched_at': row['fetched_at'],
    }
