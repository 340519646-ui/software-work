"""一级抽取：正则 + 词表（规则优先）。

职责边界
--------
纯函数模块：输入 ``CleanArticle``，输出 ``Dict[字段名, FieldHit]``。
不发请求、不调用 LLM、不写文件。

硬性约定
--------
1. 只处理 ``contracts.CORE_FIELDS`` 中的七项字段；键越界由 ``FieldHit`` 直接报错。
2. **只产出命中**：拿不到值的字段不要放进返回值（不要造 ``value="未知"`` 的命中），
   「未知」由 ``JobRecord`` 的默认值统一承担。
3. 每个命中的 ``FieldHit.method`` 必须是 ``ExtractMethod.RULE``，
   且 ``evidence`` 填命中的**原文片段**（留空即视为不合格命中）。
4. 词表与正则必须来自配置（``cfg.extract.lexicon_file`` / ``regex_file``），
   允许内置少量兜底规则，但不得把站点相关词表硬编码在函数体里。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Pattern, Sequence, Tuple

from src.config import AppConfig
from src.contracts import MISSING, CleanArticle, ExtractMethod, FieldHit, normalize_missing

EVIDENCE_WINDOW = 14
"""命中证据向两侧扩展的字符数，保证人工复核时能看清上下文。"""

LOCATION_PREFIXES: tuple = ("在", "于", "了", "的")

EMPLOYER_CUES: str = (
    "现任|任职于|就职于|供职于|工作于|服务于|任职|签约|入职|进入|加入|被录取到|拿到"
)
"""单位抽取的线索词（可从 aliases.yaml 的 employer_cues 覆盖）。

真实校友分享文章的写法是「现任 XX 公司 总经理」，只有「签约/入职」是不够的。
"""

POSITION_CUES: str = "担任|岗位为|职位为|岗位是|任职|从事|现任"
"""岗位抽取的线索词。"""

# 城市前缀只有在后面跟这些字时才剥离：否则「北京满分进出口贸易有限公司」会被剥成「满分…公司」
CITY_STRIP_FOLLOWERS: tuple = ("的", "一", "某", "该", "这", "那")

CITY_FALSE_SUFFIXES: tuple = (
    "大学",
    "学院",
    "公司",
    "集团",
    "银行",
    "研究院",
    "研究所",
    "设计院",
    "医院",
    "中学",
    "小学",
    "航空",
    "理工",
    "师范",
    "民族",
)
"""城市名后面紧跟这些词时，说明它是机构名的一部分（实测「北京大学」被误判为城市「北京」）。"""
"""单位/岗位命中结果开头常见的连接词，需要剥掉。"""

SUFFIX_GROUP = (
    "有限公司|股份有限公司|有限责任公司|集团有限公司|科技有限公司|公司|集团|银行|"
    "证券|保险|研究院|研究所|设计院|科学院|大学|学院|中学|小学|医院|事务所|局|委员会|中心|事业部"
)

POSITION_KEYWORD_GROUP = (
    "算法工程师|开发工程师|软件工程师|前端工程师|后端工程师|测试工程师|运维工程师|"
    "数据分析师|产品经理|项目经理|客户经理|助理工程师|管理培训生|"
    "总经理|副总经理|总监|主管|董事长|创始人|合伙人|总裁|副总裁|首席代表|"
    "主任|处长|科长|会长|主席|"
    "工程师|经理|助理|专员|教师|研究员|设计师|分析师|顾问|会计|律师|公务员|管培生|实习生"
)
"""后缀/岗位关键词的内置兜底；词表文件存在时以词表为准（见 employer_suffix / position_keywords）。"""



def load_lexicon(path: str) -> Mapping[str, Dict[str, Tuple[str, ...]]]:
    """读取别名/词表文件（默认 ``config/aliases.yaml``）。

    返回结构：``{分组名: {标准值: (别名1, 别名2, ...)}}``，
    例如 ``{"cities": {"北京": ("北京市", "京"), ...}}``。
    """
    try:
        # 解析层不直接读 YAML：统一走配置层入口（契约测试会拦住越界依赖）
        from src.config import load_lexicon as _load_lexicon_at_config_layer

        loaded = _load_lexicon_at_config_layer(path)
    except Exception:  # pragma: no cover - 词表损坏时降级为「无词表」
        return {}
    if not isinstance(loaded, Mapping):
        return {}

    result: Dict[str, Dict[str, Tuple[str, ...]]] = {}
    for group, value in loaded.items():
        if isinstance(value, Mapping):
            # {标准值: [别名...]} 形态（cities / degree / grade / major ...）
            normalized: Dict[str, Tuple[str, ...]] = {}
            for standard, aliases in value.items():
                if aliases is None:
                    normalized[str(standard)] = ()
                elif isinstance(aliases, (list, tuple)):
                    normalized[str(standard)] = tuple(str(item) for item in aliases)
                else:
                    normalized[str(standard)] = (str(aliases),)
            result[str(group)] = normalized
        elif isinstance(value, (list, tuple)):
            # [词, 词, ...] 形态（employer_suffix / position_keywords / employer_cues / missing_tokens）
            result[str(group)] = {str(item): () for item in value}
    return result


def build_patterns(cfg: AppConfig) -> Mapping[str, Sequence[Pattern[str]]]:
    """按字段构造正则集合（编译一次、复用），只覆盖规则可稳定判定的字段。

    建议内置骨架（本人按门户实际表述补齐）：
      * ``graduation_year``：``(\\d{4})\\s*届``、``20\\d{2}\\s*年\\s*毕业``
      * ``grade``：``(大一|大二|大三|大四|研一|研二|研三|应届)
      * ``degree``：``(博士|硕士|本科|专科|大专)``
      * ``employer``：``(?:签约|就职于|入职|进入)\\s*([^，。；\\s]{2,30})``
      * ``position``：``(?:岗位|职位|担任)\\s*([^，。；\\s]{2,20})``
    """
    # 线索词与关键词优先取词表（aliases.yaml），没有则用内置兜底——
    # 之前是硬编码，导致配置文件里的 employer_cues / position_keywords 形同虚设。
    lexicon = dict(cfg.lexicon) or load_lexicon(str(cfg.path(cfg.extract.lexicon_file)))
    suffix_group = _alternation(lexicon.get("employer_suffix", {}), SUFFIX_GROUP)
    cue_group = _alternation(lexicon.get("employer_cues", {}), EMPLOYER_CUES)
    keyword_group = _alternation(lexicon.get("position_keywords", {}), POSITION_KEYWORD_GROUP)

    patterns: Dict[str, List[Pattern[str]]] = {
        "graduation_year": [
            re.compile(r"(\d{4})\s*届"),
            # 真实校友分享常用「2001级」（不是「2001届」）——实测漏抽过
            re.compile(r"(\d{4})\s*级"),
            re.compile(r"(\d{4})\s*年\s*毕业"),
        ],
        "grade": [
            re.compile(r"(大一|大二|大三|大四|研一|研二|研三|应届毕业生|应届生|应届)"),
        ],
        "degree": [
            re.compile(r"(博士研究生|博士|硕士研究生|硕士|研究生|本科|专科|大专|专升本)"),
        ],
        "employer": [
            re.compile(
                r"(?:" + cue_group + r")([\u4e00-\u9fa5A-Za-z0-9()（）·]{2,30}?(?:" + suffix_group + r"))"
            ),
        ],
        "position": [
            # 结构：线索词 + 【可选且不捕获】公司名 + 捕获(前缀词 + 职务)
            # 实测：没有这个可选公司名前缀时，「现任北京满分进出口贸易有限公司总经理」
            # 会把整个公司名吞进岗位值里。
            re.compile(
                r"(?:" + POSITION_CUES + r")"
                r"(?:[\u4e00-\u9fa5A-Za-z0-9()（）·]{2,30}?(?:" + suffix_group + r"))?"
                r"([\u4e00-\u9fa5A-Za-z0-9]{0,12}?(?:" + keyword_group + r"))"
            ),
        ],
    }

    # 站点级自定义正则（可选）：config/config.yaml 的 extract.regex_file
    if cfg.extract.regex_file:
        extra_path = cfg.path(cfg.extract.regex_file)
        if extra_path.exists():
            from src.config import load_yaml

            extra = load_yaml(extra_path)
            if isinstance(extra, Mapping):
                for field, values in extra.items():
                    if field not in patterns or not isinstance(values, (list, tuple)):
                        continue
                    for value in values:
                        try:
                            patterns[field].append(re.compile(str(value)))
                        except re.error:  # pragma: no cover - 跳过写错的正则
                            continue
    return patterns


def extract_by_rules(article: CleanArticle, cfg: AppConfig) -> Dict[str, FieldHit]:
    """一级抽取主入口。

    实现顺序建议：先词表精确匹配（城市/学历/届别/年级），再正则就近匹配（单位/岗位），
    同一字段多次命中时取**证据最长**的一条，保证可解释性。
    仅当 ``cfg.extract.rule_first`` 为真时被调用（该开关在配置校验中被强制为真）。
    """
    cfg_patterns = build_patterns(cfg)
    # 词表在配置阶段已读好并注入（每篇文章零文件 IO）；为空时再兜底读一次
    lexicon = dict(cfg.lexicon) or load_lexicon(str(cfg.path(cfg.extract.lexicon_file)))

    headline = re.sub(r"\s+", "", article.title or article.segments.get("headline", "") or "")
    body = re.sub(r"\s+", "", article.text or article.segments.get("raw", "") or "")
    both = headline + body
    if not both:
        return {}

    cities = lexicon.get("cities", {})
    hits: Dict[str, FieldHit] = {}

    def record(field: str, value: str, evidence: str, confidence: float = 1.0) -> None:
        """统一写入口径：值先折叠缺失，已命中不覆盖，缺证据不采信。"""
        cleaned = normalize_missing(value)
        if field in hits or cleaned == MISSING or not str(evidence or "").strip():
            return
        hits[field] = FieldHit(field, cleaned, ExtractMethod.RULE, evidence, confidence)

    # ---- 届别：年份正则优先（证据最明确），词表兜底 ----
    matched, evidence = match_first(body, cfg_patterns.get("graduation_year", ()))
    if not matched:
        matched, evidence = match_first(headline, cfg_patterns.get("graduation_year", ()))
    if matched:
        digits = re.search(r"\d{4}", matched)
        if digits:
            record("graduation_year", digits.group(0), evidence)

    # ---- 词表可稳定判定的四类：年级 / 学历 / 专业 / 城市 ----
    for field, group in (
        ("grade", "grade"),
        ("degree", "degree"),
        ("major", "major"),
    ):
        standard, evidence = _best_match(match_lexicon(both, lexicon.get(group, {})))
        record(field, standard, evidence)

    # 城市单独走位置感知匹配：避免「北京大学」被抽成城市「北京」（实测踩到过）
    standard, evidence = match_city(both, lexicon.get("cities", {}))
    record("city", standard, evidence)

    # ---- 单位：动词线索 + 单位后缀 ----
    matched, evidence = match_first(body, cfg_patterns.get("employer", ()))
    if matched:
        captured = _strip_location(matched, cities)
        record("employer", captured, evidence)

    # ---- 岗位：必须带线索词（担任/岗位为/职位为/任职/从事）----
    # 不做"全文找岗位关键词"的兜底：实测那会产生假命中
    # （如「教师资格培训」→ 岗位=教师、「前公务员考官」→ 岗位=公务员），
    # 这些噪声比"少抽一个字段"更糟——误召会污染统计与抽检结论。
    matched, evidence = match_first(body, cfg_patterns.get("position", ()))
    if matched:
        record("position", _strip_location(matched, cities), evidence)

    return hits


def match_first(text: str, patterns: Sequence[Pattern[str]]) -> Tuple[str, str]:
    """辅助（纯函数）：按顺序尝试正则，返回 ``(命中文本, 证据片段)``；全不中返回 ``("", "")``。"""
    if not text:
        return "", ""
    for pattern in patterns:
        found = pattern.search(text)
        if not found:
            continue
        captured = found.group(1) if found.groups() else found.group(0)
        return captured, _window(text, found.start(), found.end())
    return "", ""


def match_lexicon(text: str, lexicon: Mapping[str, Tuple[str, ...]]) -> Dict[str, str]:
    """辅助（纯函数）：在一段文本上做词表匹配，返回 ``{标准值: 证据片段}``。

    按「标准值 + 别名」的长度**从长到短**匹配：先命中的更具体，
    这样「北京市」不会被「京」截断，长专业名也不会被短专业名抢占。
    """
    if not text or not lexicon:
        return {}

    candidates: List[Tuple[str, str]] = []
    for standard, aliases in lexicon.items():
        for alias in (standard, *(aliases or ())):
            alias = str(alias or "").strip()
            if alias:
                candidates.append((alias, str(standard)))
    candidates.sort(key=lambda item: (-len(item[0]), item[0]))

    found: Dict[str, str] = {}
    for alias, standard in candidates:
        if standard in found:
            continue
        index = text.find(alias)
        if index < 0:
            continue
        found[standard] = _window(text, index, index + len(alias))
    return found


def match_city(text: str, lexicon: Mapping[str, Tuple[str, ...]]) -> Tuple[str, str]:
    """城市匹配（**位置感知**）：排除「北京大学」「北京银行」这类机构名里的城市字样。

    返回 ``(标准城市名, 证据片段)``；找不到返回 ``("", "")``。
    """
    if not text or not lexicon:
        return "", ""

    candidates: List[Tuple[str, str]] = []
    for standard, aliases in lexicon.items():
        for alias in (standard, *(aliases or ())):
            alias = str(alias or "").strip()
            if alias:
                candidates.append((alias, str(standard)))
    candidates.sort(key=lambda item: (-len(item[0]), item[0]))

    for alias, standard in candidates:
        start = text.find(alias)
        while start >= 0:
            tail = text[start + len(alias) : start + len(alias) + 4]
            if not any(tail.startswith(suffix) for suffix in CITY_FALSE_SUFFIXES):
                return standard, _window(text, start, start + len(alias))
            start = text.find(alias, start + 1)
    return "", ""


def _alternation(lexicon_group: Mapping[str, Tuple[str, ...]], fallback: str) -> str:
    """把「内置兜底词 + 词表词」拼成正则候选串（长词在前，避免短词抢先匹配）。

    **并集而非替换**：词表是"增量补充"（例如补上本校常见的单位后缀），
    如果把内置词整体换掉，aliases.yaml 里没写的通用词（工程师/经理/助理…）就会消失，
    反而让原本能匹配的表述失配（实测踩到过）。
    """
    builtin = [item.strip() for item in str(fallback).split("|") if item.strip()]
    configured = [str(word).strip() for word in (lexicon_group or {}) if str(word).strip()]
    items = sorted(set(builtin) | set(configured), key=len, reverse=True)
    return "|".join(items)


def _best_match(candidates: Mapping[str, str]) -> Tuple[str, str]:
    """从词表命中结果里挑最可信的一条：别名越长越具体，同长按标准值排序保证确定性。"""
    if not candidates:
        return "", ""
    standard, evidence = sorted(candidates.items(), key=lambda item: (-len(item[1]), item[0]))[0]
    return standard, evidence


def _window(text: str, start: int, end: int) -> str:
    """截取命中位置两侧各 ``EVIDENCE_WINDOW`` 字符作为证据。"""
    left = max(0, start - EVIDENCE_WINDOW)
    right = min(len(text), end + EVIDENCE_WINDOW)
    return text[left:right].strip()


def _strip_location(value: str, cities: Mapping[str, Tuple[str, ...]]) -> str:
    """剥掉单位/岗位名开头的连接词与城市前缀，如「北京的某某科技有限公司」→「某某科技有限公司」。"""
    cleaned = str(value or "").strip()
    for _ in range(3):
        before = cleaned
        for prefix in LOCATION_PREFIXES:
            if cleaned.startswith(prefix) and len(cleaned) > len(prefix) + 1:
                cleaned = cleaned[len(prefix):]

        for city in sorted(cities, key=len, reverse=True):
            if not cleaned.startswith(city) or len(cleaned) <= len(city) + 1:
                continue
            follower = cleaned[len(city) : len(city) + 1]
            # 只有「北京的X」「北京一家X」「北京某X」这类才把城市当修饰语剥掉；
            # 「北京满分进出口贸易有限公司」里的「北京」是公司名的一部分，必须保留（实测踩到过）。
            if follower in CITY_STRIP_FOLLOWERS:
                cleaned = cleaned[len(city):]
                break
        if cleaned == before:
            break
    return cleaned
