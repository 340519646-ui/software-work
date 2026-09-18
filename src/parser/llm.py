"""二级抽取：LLM JSON 兜底。

职责边界
--------
只在**一级规则未命中**的字段上工作，输入 ``CleanArticle`` + 待补字段名，
输出 ``Dict[字段名, FieldHit]``。禁止用 LLM 覆盖已有规则命中值。

输出协议（硬约束，契约测试会检查键名）
-----------------------------------
1. 要求模型**只**返回 JSON，键固定为待补字段名，值为
   ``{"value": "...", "evidence": "原文片段"}``；
2. 无法判定的字段值必须是 ``"未知"``（而不是 null / 空串 / 编造）；
3. ``evidence`` 必须能在正文中找到（实现时应做子串校验，找不到即丢弃该字段）；
4. 返回的每个 ``FieldHit`` 的 ``method`` 必须是 ``ExtractMethod.LLM``。

错误约定
--------
* 未启用 / 缺少 Key 等配置问题 → ``ConfigError``（由 ``cfg.require_llm()`` 抛）；
* 网络或鉴权失败 → ``LlmError``，**不抛出致命错误**给整批，由抽取层降级为
  「保持未知 + 记 llm_failed 计数」；
* 返回内容不是合法 JSON 或缺 evidence → ``LlmError(code=LLM_BAD_RESPONSE)``。
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Mapping, Sequence, Tuple

from src.config import AppConfig
from src.contracts import (
    CORE_FIELDS,
    CORE_FIELD_LABELS,
    MISSING,
    CleanArticle,
    ExtractMethod,
    FieldHit,
    LlmError,
    normalize_missing,
)

PROMPT_BODY_LIMIT = 6000
"""提示词里正文的最大字符数；超长时保留头尾各一半（截断口径写入 docs/experiment.md）。"""


MAX_EVIDENCE_CHARS = 80
"""evidence 截断长度，保证导出报表可读。"""


def is_available(cfg: AppConfig) -> bool:
    """LLM 是否可用：开关打开且配置完整（不探测网络）。"""
    if not cfg.llm.enabled:
        return False
    if not cfg.llm.base_url.strip() or not cfg.llm.model.strip():
        return False
    if cfg.llm.provider == "openai" and not cfg.llm.api_key.strip():
        return False

    module = "openai" if cfg.llm.provider == "openai" else "ollama"
    try:
        __import__(module)
    except ImportError:
        return False
    return True


def build_prompt(
    article: CleanArticle,
    missing_fields: Sequence[str],
    cfg: AppConfig,
) -> Tuple[str, str]:
    """构造 ``(system, user)`` 提示词（纯函数，便于单测提示词内容）。

    提示词必须包含：七项字段的中文口径（``contracts.CORE_FIELD_LABELS``）、
    待补字段清单、必须给出 evidence 的要求、无法判定时输出「未知」的要求。
    正文过长时按字节截断（建议保留头尾各一半），截断规则写进 docs/experiment.md。
    """
    labels = "、".join(
        f"{field}（{CORE_FIELD_LABELS.get(field, field)}）" for field in missing_fields
    )
    system = "你是严谨的信息抽取助手。只输出一个 JSON 对象，不要输出解释，也不要用代码块包裹。"

    body = re.sub(r"\s+", " ", article.text or "").strip()
    if len(body) > PROMPT_BODY_LIMIT:
        half = PROMPT_BODY_LIMIT // 2
        body = f"{body[:half]}\n……（中间省略 {len(body) - PROMPT_BODY_LIMIT} 字）……\n{body[-half:]}"

    user = (
        f"从下面这篇就业分享文章中抽取这些字段：{labels}。\n\n"
        "输出格式：一个 JSON 对象，键是上面列出的字段名，值是同时包含 value 与 evidence 两个键的对象；"
        "value 是抽取结果，evidence 是原文中支撑该结论的连续片段。\n\n"
        "硬性要求：\n"
        "1. 只输出上面列出的字段，不要增删字段；\n"
        "2. evidence 必须是原文中原样出现的连续片段，不允许改写、拼接或翻译；\n"
        "3. 无法判断的字段，value 填「未知」，evidence 填空字符串；\n"
        "4. 不要推测，不要编造。\n\n"
        f"文章标题：{article.title}\n文章正文：{body}"
    )
    return system, user


def parse_llm_response(payload: str, article: CleanArticle) -> Dict[str, FieldHit]:
    """纯函数：把模型返回文本解析成 ``FieldHit`` 字典。

    要求：
      * 容忍 ```json 代码块包裹，其余情况严格 ``json.loads``；
      * 过滤掉非法字段名、空值、值为「未知」的项；
      * 校验 ``evidence`` 确实是 ``article.text`` 的子串，否则丢弃该项（防幻觉）；
      * 结果非空但全部被丢弃时，可抛 ``LlmError(LLM_BAD_RESPONSE)``。
    """
    text = (payload or "").strip()
    if not text:
        raise LlmError("LLM 返回为空", detail="empty payload")

    # 容忍 ```json ... ``` 包裹
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise LlmError("LLM 返回不是合法 JSON", detail=f"{exc}; payload={text[:200]!r}") from exc
    if not isinstance(data, Mapping):
        raise LlmError("LLM 返回的 JSON 顶层必须是对象", detail=f"got {type(data).__name__}")

    body = re.sub(r"\s+", "", article.text or "")
    hits: Dict[str, FieldHit] = {}

    for field, item in data.items():
        if field not in CORE_FIELDS or field in hits:
            continue
        if isinstance(item, Mapping):
            raw_value = item.get("value", "")
            evidence = str(item.get("evidence", "") or "")
        else:
            raw_value = item
            evidence = ""

        value = normalize_missing(raw_value)
        if value == MISSING:
            continue

        compact_evidence = re.sub(r"\s+", "", evidence)
        if not compact_evidence or compact_evidence not in body:
            # 证据在正文里找不到 → 视为幻觉，直接丢弃该字段（宁缺勿假）
            continue
        hits[field] = FieldHit(field, value, ExtractMethod.LLM, evidence.strip())

    return hits


def extract_by_llm(
    article: CleanArticle,
    missing_fields: Sequence[str],
    cfg: AppConfig,
) -> Dict[str, FieldHit]:
    """调用 LLM 补齐 ``missing_fields``，返回命中的 ``FieldHit``。

    实现要求：``temperature`` 取 ``cfg.llm.temperature``（默认 0）、
    重试不超过 ``cfg.llm.max_retries``、单次超时 ``cfg.llm.timeout``。
    """
    fields: List[str] = [field for field in missing_fields if field in CORE_FIELDS]
    if not fields:
        return {}

    cfg.require_llm()
    if not is_available(cfg):
        raise LlmError(
            f"LLM 依赖未就绪（provider={cfg.llm.provider}）：请安装对应 SDK，或改用本地 ollama"
        )

    system, user = build_prompt(article, fields, cfg)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    last_error: LlmError = LlmError("LLM 调用失败")
    for _ in range(max(1, int(cfg.llm.max_retries) + 1)):
        try:
            payload = _call_provider(messages, cfg)
            return parse_llm_response(payload, article)
        except LlmError as exc:
            last_error = exc
        except Exception as exc:  # 网络/鉴权等一律降级为 LlmError
            last_error = LlmError("LLM 调用失败", detail=str(exc))
    raise last_error


def _call_provider(messages: Sequence[Mapping[str, str]], cfg: AppConfig) -> str:
    """实际发起请求：openai 兼容接口 或 本地 ollama。"""
    if cfg.llm.provider == "openai":
        from openai import OpenAI

        client = OpenAI(
            api_key=cfg.llm.api_key,
            base_url=cfg.llm.base_url,
            timeout=cfg.llm.timeout,
        )
        response = client.chat.completions.create(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            messages=list(messages),
        )
        content = response.choices[0].message.content or ""
        return str(content)

    import ollama

    client = ollama.Client(host=cfg.llm.base_url)
    response = client.chat(
        model=cfg.llm.model,
        messages=list(messages),
        options={"temperature": cfg.llm.temperature},
    )
    message = response.get("message") if isinstance(response, Mapping) else getattr(response, "message", None)
    if isinstance(message, Mapping):
        content = message.get("content", "")
    else:
        content = getattr(message, "content", "")
    return str(content or "")
