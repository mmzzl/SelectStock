""""持仓诊断 analyze —— M1 契约锚（TDD RED→GREEN 自包含，零跨用例引用）。

对齐《2-红线契约清单》第一章节-资产诊断/判定收敛插件 + REL-07：
- R1-1 判定收敛插件：后端 `POST /api/portfolio/analyze` 仅代理插件受理，零判定零计算；
  出参 {run_id, status, results, summary}；M1 同步代理成功后 status=completed。
- R2-2 幂等重放：同 (user_id, user_holdings_md5, natural_day) → 复用既有 run_id。
- R2-1 属主隔离：另一用户同名仓位 → 不同 run_id。

fixture `client` 来自 backend/tests/conftest.py（复用真 app 工厂，一次 create_app，零自建 transport）。
两用例各自完全自包含：体内只引用模块常量 + 本用例体内的局部，零跨用例引用。
"""

import pytest

ANALYZE_BODY = {
    "user_id": "user_m1_diag_p0",
    "user_holdings_md5": "md5_m1_diag_p0",
    "natural_day": "2026-09-16",
    "holdings": [
        {"code": "600519", "name": "贵州茅台", "quantity": 100, "cost_price": 1688.0},
        {"code": "000858", "name": "五粮液", "quantity": 50, "cost_price": 142.0},
    ],
}
OTHER_USER_ID = "user_m1_diag_other_user"


async def _analyze(client, body):
    r = await client.post("http://test/api/portfolio/analyze", json=body)
    return r.status_code, r.json()


async def test_analyze_contract_proxy_and_replay(client):
    """R1-1 代理受理 + R2-2 幂等重放（同键→复用 run_id）。"""
    s1, j1 = await _analyze(client, ANALYZE_BODY)
    assert s1 == 200, f"analyze 应 200：got {s1}: {j1}"
    data1 = j1["data"]
    assert data1["run_id"]
    assert data1["status"] == "completed"

    s2, j2 = await _analyze(client, ANALYZE_BODY)
    assert s2 == 200, j2
    assert j2["data"]["run_id"] == data1["run_id"]


async def test_analyze_user_isolation(client):
    """R2-1 属主隔离：另一用户 → 不同 run_id。"""
    s3, j3 = await _analyze(client, ANALYZE_BODY)
    assert s3 == 200, j3
    run_self = j3["data"]["run_id"]
    assert run_self

    s4, j4 = await _analyze(client, {**ANALYZE_BODY, "user_id": OTHER_USER_ID})
    assert s4 == 200, j4
    assert j4["data"]["run_id"] != run_self
