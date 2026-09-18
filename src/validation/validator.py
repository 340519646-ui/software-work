"""校验：字段合法性、必填判定、去重。实现 ``contracts.RecordValidator``。

职责边界
--------
只判定、不修改：``validate()`` 返回 ``ValidationResult``，不得就地改写记录内容
（``review_status`` 由人工复核环节回写，不由本模块设置）。

判定口径（全项目唯一）
--------------------
| 情况 | code | severity | 结果 |
| --- | --- | --- | --- |
| 核心字段为「未知」 | ``EXTRACT_INCOMPLETE`` | WARNING | ``ok=True``，但进待人工清单 |
| 必填字段（fields.yaml required）缺失 | ``FIELD_INVALID`` | ERROR | ``ok=False`` |
| 届别不是 4 位年份或超出合理区间 | ``FIELD_OUT_OF_RANGE`` | ERROR | ``ok=False`` |
| 学历/城市不在词表内 | ``FIELD_INVALID`` | WARNING | 仅提示，人工确认 |
| ``source_url`` 不是 http(s) | ``FIELD_INVALID`` | ERROR | ``ok=False``，不可溯源即不可入库 |
| 与已有记录 ``source_url`` 重复 | ``DUPLICATE`` | ERROR | ``ok=False``，由去重环节消化 |

「未知 → 进待人工清单」是**警告**而非错误：缺失字段不阻塞入库（数据集价值在于可追溯），
这与 README「缺失标未知进待人工清单」的口径一致。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from src.config import AppConfig
from src.contracts import (
    CORE_FIELDS,
    MISSING,
    ContentKind,
    ErrorCode,
    JobRecord,
    RecordValidator,
    Severity,
    ValidationIssue,
    ValidationResult,
)

MIN_GRADUATION_YEAR = 1990
"""届别合理区间下限：早于该年份的「届别」一律视为解析错误，而非真实数据。"""



class DefaultValidator:
    """默认校验器：按上表口径逐条检查。"""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._lexicon = self._load_lexicon()

    # ---------- 词表（只读 aliases.yaml 的标准值，不依赖 parser 层） ----------

    def _load_lexicon(self) -> Dict[str, Set[str]]:
        """读取词表标准值集合，用于「值不在词表内」的软校验。

        只取分组内字典的**键**（标准值）；读不到文件或格式异常时返回空字典，
        对应校验自动跳过——校验器不该因为词表缺失而阻断整批。
        """
        path = self._cfg.path(self._cfg.extract.lexicon_file)
        if not path.exists():
            return {}
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # pragma: no cover - 词表损坏时降级
            return {}
        result: Dict[str, Set[str]] = {}
        for group in ("cities", "degree", "grade", "graduation_year"):
            value = loaded.get(group)
            if isinstance(value, Mapping):
                result[group] = {str(key) for key in value.keys()}
        return result

    def validate(self, record: JobRecord) -> ValidationResult:
        issues: List[ValidationIssue] = []
        issues.extend(self.check_required(record))
        issues.extend(self.check_formats(record))
        issues.extend(self.check_unknowns(record))
        issues.extend(self.check_content_kind(record))
        ok = not any(issue.severity is Severity.ERROR for issue in issues)
        return ValidationResult(record=record, ok=ok, issues=tuple(issues))

    def check_required(self, record: JobRecord) -> Tuple[ValidationIssue, ...]:
        """按 ``cfg.required_columns`` 检查必填列是否为空。"""
        issues: List[ValidationIssue] = []
        for column in self._cfg.required_columns:
            value = getattr(record, column, "")
            if not str(value or "").strip():
                issues.append(
                    ValidationIssue(
                        code=ErrorCode.FIELD_INVALID,
                        message=f"必填列 {column} 为空：缺溯源信息，该记录不可入库",
                        field_name=column,
                    )
                )
        return tuple(issues)

    def check_formats(self, record: JobRecord) -> Tuple[ValidationIssue, ...]:
        """格式与区间校验：届别年份、source_url、学历/城市词表（词表口径由本人确认）。"""
        issues: List[ValidationIssue] = []

        # 届别：必须是 4 位年份且落在合理区间
        year = record.field_value("graduation_year")
        if year != MISSING:
            if not re.fullmatch(r"\d{4}", year):
                issues.append(
                    ValidationIssue(
                        code=ErrorCode.FIELD_OUT_OF_RANGE,
                        message=f"届别「{year}」不是 4 位年份",
                        field_name="graduation_year",
                    )
                )
            else:
                upper = datetime.now().year + 1
                if not (MIN_GRADUATION_YEAR <= int(year) <= upper):
                    issues.append(
                        ValidationIssue(
                            code=ErrorCode.FIELD_OUT_OF_RANGE,
                            message=f"届别 {year} 超出合理区间 {MIN_GRADUATION_YEAR}~{upper}",
                            field_name="graduation_year",
                        )
                    )

        # 溯源红线：source_url 必须是绝对地址，否则「可溯源」无从谈起
        url = str(getattr(record, "source_url", "") or "")
        if url and not url.startswith(("http://", "https://")):
            issues.append(
                ValidationIssue(
                    code=ErrorCode.FIELD_INVALID,
                    message=f"source_url 必须是 http(s) 绝对地址，当前为 {url!r}",
                    field_name="source_url",
                )
            )

        # 词表软校验（WARNING）：不在词表内不阻塞入库，但提示人工确认
        for field_name, group, label in (
            ("degree", "degree", "学历"),
            ("city", "cities", "城市"),
            ("grade", "grade", "年级"),
        ):
            value = record.field_value(field_name)
            allowed = self._lexicon.get(group)
            if value != MISSING and allowed and value not in allowed:
                issues.append(
                    ValidationIssue(
                        code=ErrorCode.FIELD_INVALID,
                        message=f"{label}「{value}」不在词表内，请人工确认是否为真实取值",
                        severity=Severity.WARNING,
                        field_name=field_name,
                    )
                )
        return tuple(issues)

    def check_unknowns(self, record: JobRecord) -> Tuple[ValidationIssue, ...]:
        """把「未知」核心字段转成 WARNING 级别的 ``EXTRACT_INCOMPLETE`` 问题。"""
        return tuple(
            ValidationIssue(
                code=ErrorCode.EXTRACT_INCOMPLETE,
                message=f"{field} 未抽取到（规则与 LLM 均未命中），已记「未知」并进待人工清单",
                severity=Severity.WARNING,
                field_name=field,
            )
            for field in record.unknown_fields()
        )

    def check_content_kind(self, record: JobRecord) -> Tuple[ValidationIssue, ...]:
        """图片 OCR 来源的记录强制走人工复核（合规红线 5）。"""
        if record.content_kind == ContentKind.OCR.value:
            return (
                ValidationIssue(
                    code=ErrorCode.OCR_NEEDS_REVIEW,
                    message="内容来自图片 OCR：必须人工复核后才能采信（合规红线 5）",
                    severity=Severity.WARNING,
                    field_name="content_kind",
                ),
            )
        return ()


def validate_batch(records: Iterable[JobRecord], cfg: AppConfig) -> List[ValidationResult]:
    """批量校验（顺序与输入一致；不抛异常，问题都在结果里）。"""
    validator = DefaultValidator(cfg)
    return [validator.validate(record) for record in records]


def deduplicate(records: Iterable[JobRecord]) -> List[JobRecord]:
    """按 ``source_url`` 去重（保留首次出现），并忽略没有 source_url 的记录。

    这是**幂等键去重**的唯一实现：pipeline 与 storage 都不得再写第二套去重逻辑。
    """
    seen: Set[str] = set()
    unique: List[JobRecord] = []
    for record in records:
        key = str(record.source_url or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def needs_manual_review(result: ValidationResult) -> bool:
    """是否进待人工清单：委托 ``ValidationResult.needs_manual_review``（不重复判定）。"""
    return result.needs_manual_review


def build_validator(cfg: AppConfig) -> RecordValidator:
    """工厂：pipeline 的唯一入口，返回 ``DefaultValidator(cfg)``。"""
    return DefaultValidator(cfg)
