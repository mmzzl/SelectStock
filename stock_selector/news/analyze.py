"""LLM 分析（智谱 GLM，zai-sdk）。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from stock_selector.news.schema import ANALYSIS_SCHEMA

logger = logging.getLogger("news.analyze")

SYSTEM_PROMPT = """你是一名专业的 A股 财经分析师。给你一条财经新闻，请按资深买方研究员的口吻输出分析。

注意：
1. 结论要克制、客观，区分"资本层面合作"与"利润兑现"，避免把利好简单等同于大涨。
2. 从新闻反推市场反应，明确列出「利好什么、利空什么」：利好/利空的目标既可以是板块/行业，
   也可以是相关个股。能确定个股代码的尽量填 code，填不了给空字符串。
3. 每个标的的 score 表示影响方向与强度：好处用 1~10 的正数，坏处用 -10~-1 的负数。
   同一板块内相关个股的评分可不同（直接受益者分更高，间接受益者分更低）。
4. 额外给出「当前应该关注哪些」：watchlist 是现在就该盯着的标的（结合短期情绪强度给优先级
   高/中/低）；follow_signals 是后续需要验证的信号（订单落地、盈利拐点、板块联动等），
   action 用一两句话说明当下该怎么做（关注、观望、等回踩、等信号确认等）。
5. 必须提示利好兑现的风险：risks 列出"利好不会马上兑现"的障碍（如标的基本面仍亏损、投的是
   子公司而非上市公司本体、估值已透支、限售解禁、定增摊薄等），每项说明 impact(对股价/操作的
   影响)和 mitigation(什么条件下风险可解除/如何规避)。score 用 -10~-1 表示风险强度。
6. 只输出一个 JSON 对象，不要输出任何多余文字或 markdown 代码块标记。
7. 若问题中提供「已知相关标的」，bullish/bearish/watchlist 的 code 应优先沿用这些已知代码；
   无法确定时给空字符串，不要编造代码。

JSON 结构：
{
  "summary": "一句话概括事件要点（含关键数字，如金额/估值/股份比例）",
  "sentiment": "利好" | "利空" | "中性",
  "sentiment_score": -10 到 10 的整数（全局净影响，利好为正，利空为负）,
  "impact_period": "短期" | "中期" | "长期",
  "bullish": [
    {"category": "板块 | 行业 | 个股", "name": "二线动力电池", "code": "", "score": 6, "reason": "..."},
    {"category": "个股", "name": "欣旺达", "code": "300207", "score": 8, "reason": "..."}
  ],
  "bearish": [{"category": "个股", "name": "同类亏损电池厂", "code": "", "score": -3, "reason": "..."}],
  "risks": [{"score": -6, "desc": "...", "impact": "...", "mitigation": "..."}],
  "watchlist": [{"category": "个股", "name": "欣旺达", "code": "300207", "priority": "高", "reason": "..."}],
  "follow_signals": ["..."],
  "action": "...",
  "key_points": ["..."],
  "market_reaction": "...",
  "conclusion": "..."
}"""


def resolve_api_key(api_key: str) -> str:
    return api_key or os.environ.get("ZHIPU_API_KEY", "")


def build_hint_lines(hints):
    lines = []
    for h in hints or []:
        if h.get("code"):
            lines.append(f"- {h['code']}（{h.get('market', '')}）")
        elif h.get("name"):
            lines.append(f"- {h['name']}（{h.get('market', '')}，代码未知）")
    return lines


def build_user_prompt(news: dict, hints: list[dict] | None = None) -> str:
    """构造 user prompt；以新闻正文(summary)为分析文本，有已知标的线索时追加代码。"""
    excerpt = (news.get("summary") or news.get("content") or news.get("title") or "")[:200]
    parts = [f"新闻正文：{excerpt}"]
    hint_lines = build_hint_lines(hints)
    if hint_lines:
        parts.append("已知相关标的（来自东财 stockList，可沿用其代码）：")
        parts.extend(hint_lines)
    parts.append("请按上述要求输出 JSON 分析。")
    return "\n".join(parts)


_client = None


def get_client(llm_cfg: dict):
    """获取（并缓存）智谱 ZhipuAiClient 单例，避免每次调用重复创建。"""
    global _client
    if _client is None:
        from zai import ZhipuAiClient

        api_key = resolve_api_key(llm_cfg.get("api_key", ""))
        if not api_key:
            raise SystemExit("缺少 GLM api_key：请在 config.yaml 的 llm.api_key 配置，或设环境变量 ZHIPU_API_KEY")

        _client = ZhipuAiClient(api_key=api_key)
    return _client


def chat_completion(llm_cfg: dict, messages: list[dict]) -> str:
    """调用智谱 zai-sdk chat.completions，返回模型文本。

    参数一律听 config.yaml 的 `llm` 节。这里曾经写死 `max_tokens=65536`、`temperature=1.0`
    并且不开思考开关，实测一条新闻 55 秒（配置写的 2048 被完全忽略）；关掉思考模式同一
    条 23 秒、token 从 2064 降到 1260，多空/风险的拆解结果不变。一晚 167 条新闻，
    差的是 2.5 小时和 6 小时。
    """
    client = get_client(llm_cfg)
    resp = client.chat.completions.create(
        model=llm_cfg["model"],
        messages=messages,
        max_tokens=int(llm_cfg.get("max_tokens", 2048)),
        temperature=float(llm_cfg.get("temperature", 0.3)),
        thinking={"type": llm_cfg.get("thinking", "disabled")},
    )
    return resp.choices[0].message.content.strip()


def _extract_json(text: str) -> dict:
    """从 LLM 输出中稳健提取 JSON 对象。"""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"输出中没有 JSON: {text[:200]}")
    return json.loads(text[start : end + 1])


def _retry_wait(exc: Exception, attempt: int) -> float:
    """限流要等一个配额窗口，普通异常 2s/4s 就够。

    6 线程并发实测把智谱账户打到 429(1302 速率限制)，而 2s 的退避正好撞在同一个窗口里，
    三条重试全部白费——一晚 18 条失败 13 条。限流必须长等。
    """
    text = str(exc)
    if "429" in text or "速率限制" in text or "1302" in text:
        return 30.0 * attempt
    return 2.0 * attempt


def analyze_news(llm_cfg: dict, news: dict, hints: list[dict] | None = None,
                 collect_date: str | None = None, retries: int = 3) -> dict:
    """对单条新闻调用 GLM，返回 ANALYSIS_SCHEMA 对应字段的 dict。"""
    user_prompt = build_user_prompt(news, hints)
    raw = None
    for attempt in range(1, retries + 1):
        try:
            raw = chat_completion(llm_cfg, [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ])
            parsed = _extract_json(raw)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("[attempt %d/%d] 分析失败: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(_retry_wait(exc, attempt))
    else:
        parsed = {
            "summary": "", "sentiment": "中性", "sentiment_score": 0,
            "impact_period": "短期", "bullish": [], "bearish": [], "risks": [],
            "watchlist": [], "follow_signals": [], "action": "", "key_points": [],
            "market_reaction": "", "conclusion": f"分析失败，LLM 原始输出: {raw}",
        }
        raw = None

    collect_date = collect_date or datetime.today().strftime("%Y-%m-%d")
    return {
        "collect_date": collect_date,
        "news_id": news.get("news_id", ""),
        "title": news.get("title", ""),
        "source": news.get("source", ""),
        "url": news.get("url", ""),
        "publish_time": news.get("publish_time"),
        "content_excerpt": (news.get("summary") or news.get("content") or "")[:200],
        "summary": parsed.get("summary", ""),
        "sentiment": parsed.get("sentiment", "中性"),
        "sentiment_score": int(parsed.get("sentiment_score", 0) or 0),
        "impact_period": parsed.get("impact_period", "短期"),
        "bullish": parsed.get("bullish", []) or [],
        "bearish": parsed.get("bearish", []) or [],
        "risks": parsed.get("risks", []) or [],
        "watchlist": parsed.get("watchlist", []) or [],
        "follow_signals": parsed.get("follow_signals", []) or [],
        "action": parsed.get("action", ""),
        "key_points": parsed.get("key_points", []) or [],
        "market_reaction": parsed.get("market_reaction", ""),
        "conclusion": parsed.get("conclusion", ""),
        "llm_model": llm_cfg["model"],
        "raw": raw,
        "stock_hints": [h.get("code") for h in (hints or []) if h.get("code")],
    }
