"""契约测试：把「模块之间的接口」变成可执行、可失败的检查。

本文件不测试业务实现（那属于 tests/test_*.py），只测试**接口本身**：

1. 契约层纯度：``src/contracts.py`` 不得 import 项目内其他模块或第三方库；
2. 分层依赖方向：parser 不得 import storage/crawler，crawler 不得 import parser，
   任何层不得 import pipeline/main（详见 ``ALLOWED_PROJECT_IMPORTS``）；
3. 第三方依赖白名单：请求库只能出现在采集层、Excel 只能出现在存储层，等等；
4. 配置注入：除 CLI 与登录自检外，任何模块都不许自己调 ``load_config``；
5. 数据契约：七项核心字段与 ``config/fields.yaml`` 一致、导出列序稳定、
   ``JobRecord`` 默认值为「未知」、``apply_hits`` 的 extract_method 归并规则唯一；
6. 幂等键：``article_key`` / 归档路径 / 清单行 → 反序列化可往返；
7. 计数键与枚举取值封闭（新增必须先改契约层）。

任何一条失败都意味着「接口被绕过」，而不是测试写错了。
"""

from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Dict, Iterator, List, Sequence, Set, Tuple

import pytest
import yaml

from src import contracts
from src.config import (
    CONFIG_RELATIVE_PATH,
    FIELDS_RELATIVE_PATH,
    ConfigError,
    LoginError,
    load_config,
)
from src.contracts import (
    CORE_FIELDS,
    CORE_FIELD_LABELS,
    COUNTER_KEYS,
    MISSING,
    PROVENANCE_FIELDS,
    ArticleRef,
    CleanArticle,
    ContentKind,
    ErrorCode,
    ExtractMethod,
    FieldHit,
    HttpResponse,
    ImageAsset,
    JobRecord,
    LoginStatus,
    ManifestEntry,
    OcrStatus,
    RawArticle,
    ReviewStatus,
    Stage,
    StageResult,
    ValidationIssue,
    ValidationResult,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

# ==========================================================================
# 依赖规则表（改接口必然改这里，改动即需 code review）
# ==========================================================================

ALLOWED_PROJECT_IMPORTS: Dict[str, Set[str]] = {
    "src": set(),
    "src.contracts": set(),
    "src.config": {"src.contracts"},
    "src.crawler": {"src.contracts", "src.config"},
    "src.parser": {"src.contracts", "src.config"},
    "src.storage": {"src.contracts", "src.config"},
    "src.validation": {"src.contracts", "src.config"},
    "src.pipeline": {"src.contracts", "src.config", "src.crawler", "src.parser", "src.storage", "src.validation"},
    "src.main": {"src.contracts", "src.config", "src.pipeline"},
}

ALLOWED_THIRD_PARTY_IMPORTS: Dict[str, Set[str]] = {
    "src": set(),
    "src.contracts": set(),
    "src.config": {"yaml", "dotenv"},
    "src.crawler": {"requests", "bs4", "lxml", "playwright", "tenacity", "urllib3"},
    "src.parser": {"bs4", "lxml", "openai", "ollama", "pytesseract", "PIL"},
    "src.storage": {"pandas", "openpyxl"},
    "src.validation": {"yaml", "pandas"},
    "src.pipeline": set(),
    "src.main": set(),
}

STDLIB_MODULES: Set[str] = {
    "__future__", "abc", "argparse", "collections", "contextlib", "csv", "dataclasses",
    "datetime", "enum", "functools", "hashlib", "html", "io", "itertools", "json", "logging",
    "math", "os", "pathlib", "posixpath", "random", "re", "secrets", "shutil", "sqlite3", "string", "sys",
    "tempfile", "time", "traceback", "typing", "unicodedata", "urllib", "uuid", "warnings",
}

# 只允许这两处调用 load_config：CLI 入口与登录自检（其余模块必须靠参数注入）
ALLOWED_LOAD_CONFIG_CALLERS: Set[str] = {
    "src/pipeline/run.py",
    "src/crawler/login_check.py",
}


def package_key(py_file: Path) -> str:
    """把文件路径映射到依赖规则表的键（``__init__.py`` 归到所在包）。"""
    rel = py_file.relative_to(REPO_ROOT).as_posix()
    parts = rel.split("/")
    if parts[0] != "src":
        return rel
    if len(parts) == 2:  # src/xxx.py
        stem = Path(parts[1]).stem
        return "src" if stem == "__init__" else f"src.{stem}"
    return "src." + parts[1]  # src/pkg/xxx.py


def iter_source_files() -> Iterator[Path]:
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def iter_imports(py_file: Path) -> Iterator[Tuple[str, str]]:
    """产出 (被 import 的模块名, 语句形式) 二元组。

    ``from src import contracts`` 会被展开成 ``src.contracts``，避免包名过粗绕过检查。
    """
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and not module:
                continue
            if module == "src":
                for alias in node.names:
                    yield f"src.{alias.name}", f"from src import {alias.name}"
                continue
            yield module, f"from {module} import ..."


def top_module(dotted: str) -> str:
    return dotted.split(".")[0]


# ==========================================================================
# 1. 契约层纯度
# ==========================================================================


def test_contracts_layer_has_no_project_or_third_party_imports() -> None:
    offenders: List[str] = []
    for module, statement in iter_imports(SRC_DIR / "contracts.py"):
        root = top_module(module)
        if root == "src":
            offenders.append(f"禁止 import 项目内模块：{statement}")
        elif root not in STDLIB_MODULES:
            offenders.append(f"契约层禁止 import 第三方库：{statement}")
    assert not offenders, "src/contracts.py 必须只依赖标准库：\n" + "\n".join(offenders)


# ==========================================================================
# 2~3. 分层依赖方向与第三方白名单
# ==========================================================================


@pytest.mark.parametrize("py_file", list(iter_source_files()), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_layering_and_dependency_whitelist(py_file: Path) -> None:
    key = package_key(py_file)
    allowed_project = ALLOWED_PROJECT_IMPORTS.get(key)
    allowed_third = ALLOWED_THIRD_PARTY_IMPORTS.get(key)
    assert allowed_project is not None, f"{key} 未在 ALLOWED_PROJECT_IMPORTS 中登记（新增层必须先登记）"
    assert allowed_third is not None, f"{key} 未在 ALLOWED_THIRD_PARTY_IMPORTS 中登记"

    violations: List[str] = []
    for module, statement in iter_imports(py_file):
        root = top_module(module)
        if root == "src":
            target = module if module.count(".") <= 1 else ".".join(module.split(".")[:2])
            if target == key or target.startswith(key + "."):
                continue  # 同层/同包内部 import 允许
            if target not in allowed_project:
                violations.append(f"违反分层方向：{statement}（{key} 只允许依赖 {sorted(allowed_project)}）")
        elif root not in STDLIB_MODULES and root not in allowed_third:
            violations.append(f"第三方依赖越界：{statement}（{key} 只允许 {sorted(allowed_third)}）")
    assert not violations, f"{py_file.relative_to(REPO_ROOT)} 接口违规：\n" + "\n".join(violations)


# ==========================================================================
# 4. 配置注入：禁止各模块自己读配置
# ==========================================================================


@pytest.mark.parametrize("py_file", list(iter_source_files()), ids=lambda p: p.relative_to(REPO_ROOT).as_posix())
def test_only_cli_and_login_check_call_load_config(py_file: Path) -> None:
    rel = py_file.relative_to(REPO_ROOT).as_posix()
    calls: List[str] = []
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
            if name == "load_config":
                calls.append(f"{rel}:{getattr(node, 'lineno', 0)}")
    if calls and rel not in ALLOWED_LOAD_CONFIG_CALLERS:
        pytest.fail(
            "只有 CLI 与登录自检可以加载配置，其余模块必须通过参数注入 AppConfig；违规点："
            + ", ".join(calls)
        )


def test_shipped_configs_are_valid_yaml_and_cover_all_sections() -> None:
    """随仓库交付的 config.yaml / fields.yaml / aliases.yaml 必须是合法 YAML 且结构完整。"""
    cfg = yaml.safe_load((REPO_ROOT / CONFIG_RELATIVE_PATH).read_text(encoding="utf-8"))
    for section in ("portal", "auth", "request", "extract", "llm", "ocr", "storage", "output", "sampling", "logging"):
        assert section in cfg, f"config.yaml 缺少配置段：{section}"
    assert cfg["request"]["interval_seconds"] >= 2, "合规红线：请求间隔不得小于 2 秒"

    fields = yaml.safe_load((REPO_ROOT / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))
    assert fields.get("fields"), "fields.yaml 必须包含 fields 列表"


# ==========================================================================
# 5. 数据契约
# ==========================================================================


def test_core_fields_are_exactly_seven_and_match_fields_yaml() -> None:
    assert len(CORE_FIELDS) == 7, "七项核心字段口径固定，不得增减"
    assert set(CORE_FIELD_LABELS) == set(CORE_FIELDS)

    declared = [f["name"] for f in yaml.safe_load((REPO_ROOT / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))["fields"]]
    for name in CORE_FIELDS:
        assert name in declared, f"fields.yaml 缺少核心字段 {name}"
    for name, label in CORE_FIELD_LABELS.items():
        spec = next(f for f in yaml.safe_load((REPO_ROOT / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))["fields"] if f["name"] == name)
        assert spec["label"] == label, f"{name} 的中文名与 contracts.CORE_FIELD_LABELS 不一致"


def test_provenance_fields_are_declared() -> None:
    declared = {
        f["name"]
        for f in yaml.safe_load((REPO_ROOT / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))["fields"]
    }
    for name in PROVENANCE_FIELDS:
        assert name in declared, f"溯源字段 {name} 必须在 fields.yaml 中声明（可溯源是本项目硬指标）"
    assert "evidence" in declared, "evidence 列必须在导出中体现，否则无法人工复核"


def test_job_record_defaults_are_unknown_and_row_follows_columns() -> None:
    record = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/1", list_url="https://example.edu.cn/list?p=1"))
    assert record.unknown_fields() == CORE_FIELDS, "空白记录必须七项全为「未知」"
    assert record.needs_manual_review() is True
    assert record.extract_method == ExtractMethod.NONE.value

    row = record.to_row(("article_key", "employer", "source_url", "evidence"))
    assert list(row.keys()) == ["article_key", "employer", "source_url", "evidence"], "导出列序必须完全遵从传入列序"
    assert row["employer"] == MISSING
    assert row["article_key"] == record.article_key
    assert row["source_url"].endswith("/a/1")


def test_apply_hits_merges_extract_method_by_single_rule() -> None:
    record = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/2"))
    record.apply_hits({"city": FieldHit("city", "北京", ExtractMethod.RULE, "签约北京某公司")})
    assert record.extract_method == ExtractMethod.RULE.value

    record.apply_hits({"employer": FieldHit("employer", "某科技有限公司", ExtractMethod.LLM, "…就职于某科技有限公司…")})
    assert record.extract_method == ExtractMethod.HYBRID.value, "规则 + LLM 同时命中必须归并为 hybrid"

    only_llm = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/3"))
    only_llm.apply_hits({"major": FieldHit("major", "软件工程", ExtractMethod.LLM, "…软件工程专业…")})
    assert only_llm.extract_method == ExtractMethod.LLM.value


def test_apply_hits_ignores_missing_values() -> None:
    record = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/4"))
    record.apply_hits({"city": FieldHit("city", "未知", ExtractMethod.RULE)})
    assert record.city == MISSING
    assert record.hits == {}, "缺失值不得成为命中"


def test_field_hit_rejects_illegal_field_and_empty_value() -> None:
    with pytest.raises(ValueError):
        FieldHit("salary", "10000", ExtractMethod.RULE, "证据")
    with pytest.raises(ValueError):
        FieldHit("city", "   ", ExtractMethod.RULE, "证据")


def test_article_ref_requires_detail_url() -> None:
    with pytest.raises(ValueError):
        ArticleRef(detail_url="  ")


# ==========================================================================
# 6. 幂等键与清单往返
# ==========================================================================


def test_article_key_and_archive_path_are_deterministic() -> None:
    url = "https://example.edu.cn/a/2025/001"
    assert contracts.article_key_of(url) == contracts.article_key_of(url)
    assert len(contracts.article_key_of(url)) == 16
    assert contracts.raw_html_relpath(url) == f"data/raw/html/{contracts.article_key_of(url)}.html"
    assert contracts.manifest_relpath() == "data/raw/manifest.jsonl"
    assert ArticleRef(detail_url=url).article_key == contracts.article_key_of(url)


def test_manifest_entry_roundtrip() -> None:
    ref = ArticleRef(detail_url="https://example.edu.cn/a/9", list_url="https://example.edu.cn/list?p=3", title="就业分享")
    raw = RawArticle.success(ref, "<html>正文</html>", ref.raw_html_path())
    entry = ManifestEntry.from_raw(raw)
    restored = ManifestEntry.from_line(entry.to_line())
    assert restored is not None
    assert restored.ref.detail_url == ref.detail_url
    assert restored.ref.title == ref.title
    assert restored.html_path == raw.html_path
    assert restored.ok is True


def test_manifest_from_line_tolerates_damage() -> None:
    assert ManifestEntry.from_line("") is None
    assert ManifestEntry.from_line("{不是 JSON") is None
    assert ManifestEntry.from_line('{"title": "缺少 detail_url"}') is None


def test_normalize_missing_covers_common_placeholders() -> None:
    for value in (None, "", "   ", "-", "—", "无", "N/A", "null"):
        assert contracts.normalize_missing(value) == MISSING
    assert contracts.normalize_missing(" 北京 ") == "北京"
    assert contracts.is_missing(MISSING) is True


# ==========================================================================
# 7. 枚举与计数键封闭
# ==========================================================================


def test_stage_values_are_closed_and_cli_parseable() -> None:
    assert [s.value for s in Stage] == ["fetch", "extract", "export"]
    assert Stage.from_cli("FETCH") is Stage.FETCH
    with pytest.raises(ValueError):
        Stage.from_cli("transform")


def test_stage_result_rejects_unknown_counter_key() -> None:
    result = StageResult.start(Stage.FETCH)
    result.add("refs_found", 3)
    assert result.counters["refs_found"] == 3
    with pytest.raises(KeyError):
        result.add("my_custom_counter")
    assert "refs_found" in COUNTER_KEYS


def test_error_codes_and_methods_are_stable() -> None:
    assert ExtractMethod.RULE.value == "rule"
    assert ExtractMethod.HYBRID.value == "hybrid"
    assert ReviewStatus.PENDING.value == "pending"
    assert ErrorCode.DUPLICATE.value == "duplicate"


def test_validation_result_manual_review_semantics() -> None:
    record = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/5"))
    incomplete = ValidationResult(record=record, ok=True, issues=(ValidationIssue(ErrorCode.EXTRACT_INCOMPLETE, "缺岗位"),))
    assert incomplete.needs_manual_review is True, "未知字段必须进待人工清单"

    complete = JobRecord.empty(ArticleRef(detail_url="https://example.edu.cn/a/6"))
    complete.apply_hits({f: FieldHit(f, f"值-{f}", ExtractMethod.RULE, "证据") for f in CORE_FIELDS})
    assert ValidationResult(record=complete, ok=True).needs_manual_review is False


def test_protocols_expose_expected_members() -> None:
    """各层实现必须逐一对上这些方法名；这里只校验契约声明本身完整。"""
    expected = {
        "Transport": {"get", "close"},
        "PageFetcher": {"fetch_list", "fetch_detail", "fetch_image", "close"},
        "ArticleExtractor": {"extract"},
        "RecordValidator": {"validate"},
        "RecordRepository": {"init_schema", "existing_source_urls", "upsert_many", "fetch_all", "export_csv", "export_xlsx", "write_manual_review", "close"},
        "StageRunner": {"run"},
    }
    for name, members in expected.items():
        protocol = getattr(contracts, name)
        declared = {m for m in dir(protocol) if not m.startswith("_")}
        missing = members - declared
        assert not missing, f"契约 {name} 缺少成员：{sorted(missing)}"


# ==========================================================================
# 配置加载与校验（属于接口行为，必须可预测）
# ==========================================================================

BASE_CONFIG: Dict[str, object] = {
    "fields_file": "config/fields.yaml",
    "portal": {"base_url": "https://example.edu.cn", "list_url": "https://example.edu.cn/jobs", "page_param": "page", "page_start": 1, "page_end": 2},
    "auth": {"method": "account", "session_file": "data/.session.json"},
    "request": {"interval_seconds": 2.0, "timeout": 20, "retries": 2},
    "extract": {"rule_first": True, "missing_placeholder": "未知", "llm_trigger_below": 7},
    "llm": {"enabled": False, "provider": "openai"},
    "ocr": {"enabled": False, "lang": "chi_sim+eng"},
    "storage": {"db_path": "data/employment.db", "batch_size": 200},
    "output": {"csv_path": "data/processed/jobs.csv", "xlsx_path": "data/processed/jobs.xlsx"},
    "sampling": {"review_rate": 0.1, "seed": 42},
    "logging": {"level": "INFO"},
}


@pytest.fixture()
def cfg_root(tmp_path: Path) -> Path:
    """构造一个最小可用的项目配置目录（真实 fields.yaml，其余用测试值）。"""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / FIELDS_RELATIVE_PATH, tmp_path / FIELDS_RELATIVE_PATH)
    _write_config(tmp_path, BASE_CONFIG)
    return tmp_path


def _write_config(root: Path, config: Dict[str, object]) -> None:
    (root / CONFIG_RELATIVE_PATH).write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")


def _patched(**sections: Dict[str, object]) -> Dict[str, object]:
    import copy

    config = copy.deepcopy(BASE_CONFIG)
    for name, patch in sections.items():
        config[name] = {**config[name], **patch}  # type: ignore[dict-item]
    return config


def test_load_config_ok_and_columns_follow_fields_yaml(cfg_root: Path) -> None:
    cfg = load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)
    declared = [f["name"] for f in yaml.safe_load((cfg_root / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))["fields"]]
    assert list(cfg.columns) == declared, "导出列序必须严格等于 fields.yaml 的声明顺序"
    assert cfg.core_fields == CORE_FIELDS
    assert "source_url" in cfg.required_columns


def test_load_config_rejects_interval_below_red_line(cfg_root: Path) -> None:
    _write_config(cfg_root, _patched(request={"interval_seconds": 1.0}))
    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)
    assert "2" in str(excinfo.value), "错误信息必须点明合规下限"


def test_load_config_rejects_bad_page_range_and_base_url(cfg_root: Path) -> None:
    _write_config(cfg_root, _patched(portal={"page_start": 5, "page_end": 2}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)

    _write_config(cfg_root, _patched(portal={"base_url": "example.edu.cn"}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)


def test_load_config_rejects_missing_file(cfg_root: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / "config" / "not-exist.yaml", project_root=cfg_root)


def test_load_config_rejects_bad_auth_method_and_placeholder(cfg_root: Path) -> None:
    _write_config(cfg_root, _patched(auth={"method": "sms"}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)

    _write_config(cfg_root, _patched(extract={"missing_placeholder": ""}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)


def test_env_overrides_yaml(cfg_root: Path) -> None:
    env = cfg_root / ".env"
    env.write_text("PORTAL_BASE_URL=https://env.example.edu.cn\nLLM_ENABLED=true\nLLM_API_KEY=sk-test\nLLM_BASE_URL=https://api.example.com/v1\nLLM_MODEL=demo\n", encoding="utf-8")
    cfg = load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root, env_path=env)
    assert cfg.portal.base_url == "https://env.example.edu.cn", ".env 必须能覆盖 yaml"
    assert cfg.llm.enabled is True


def test_require_auth_reports_missing_keys_without_leaking(cfg_root: Path) -> None:
    cfg = load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)
    with pytest.raises(LoginError) as excinfo:
        cfg.require_auth()
    assert "AUTH_STUDENT_ID" in str(excinfo.value)

    cookie_cfg = load_config(
        config_path=cfg_root / CONFIG_RELATIVE_PATH,
        project_root=cfg_root,
    )
    assert cookie_cfg.redacted()["auth"]["password"] == ""


def test_redacted_masks_credentials(cfg_root: Path) -> None:
    env = cfg_root / ".env"
    env.write_text("AUTH_STUDENT_ID=2021001234\nAUTH_PASSWORD=super-secret-password\n", encoding="utf-8")
    cfg = load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root, env_path=env)
    summary = str(cfg.redacted())
    assert "super-secret-password" not in summary
    assert "2021001234" not in summary, "学号也属于个人信息，日志中必须打码"
    cfg.require_auth()  # 凭据齐全 → 不抛异常


def test_require_llm_rejects_incomplete_config(cfg_root: Path) -> None:
    _write_config(cfg_root, _patched(llm={"enabled": True, "provider": "openai", "api_key": "", "base_url": "", "model": ""}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)


def test_llm_provider_must_be_supported(cfg_root: Path) -> None:
    _write_config(cfg_root, _patched(llm={"enabled": True, "provider": "claude", "base_url": "https://x", "model": "m", "api_key": "k"}))
    with pytest.raises(ConfigError):
        load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)


# ==========================================================================
# 1.1.0 新增契约：图片 / OCR 一等公民
# ==========================================================================


def test_content_kind_and_ocr_status_are_closed_enums() -> None:
    assert [k.value for k in ContentKind] == ["html", "ocr", "mixed"]
    assert [s.value for s in OcrStatus] == ["pending", "done", "failed", "skipped"]


def test_image_asset_paths_are_content_addressed() -> None:
    data = b"\x89PNG fake image bytes"
    detail_url = "https://example.edu.cn/a/2025/001"
    asset = ImageAsset.from_bytes(data, "https://example.edu.cn/img/1.png", detail_url)

    digest = contracts.sha1_of_bytes(data)
    assert asset.sha1 == digest
    assert asset.image_path == f"data/raw/images/{contracts.article_key_of(detail_url)}/{digest}.png"
    assert asset.ocr_cache_path == f"data/raw/ocr/{digest}.txt"
    assert asset.byte_size == len(data)
    assert asset.ocr_ready is False

    same_image_other_url = ImageAsset.from_bytes(data, "https://cdn.example/x.png", detail_url)
    assert same_image_other_url.sha1 == asset.sha1, "同一张图必须得到同一缓存键（与 URL 无关）"

    done = asset.with_ocr(OcrStatus.DONE)
    assert done.ocr_ready is True
    assert done.ocr_cache_path == asset.ocr_cache_path
    assert asset.ocr_status == OcrStatus.PENDING.value, "with_ocr 必须返回新对象，不修改原值"


def test_image_asset_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        ImageAsset(source_url="", sha1="a" * 40)
    with pytest.raises(ValueError):
        ImageAsset(source_url="https://x/1.png", sha1="abc")


def test_manifest_entry_roundtrip_with_images() -> None:
    ref = ArticleRef(detail_url="https://example.edu.cn/a/img-1", list_url="https://example.edu.cn/list?p=1")
    asset = ImageAsset.from_bytes(b"image-bytes", "https://example.edu.cn/img/1.png", ref.detail_url)
    raw = RawArticle.success(ref, "<html>图多字少</html>", ref.raw_html_path(), images=(asset,))

    entry = ManifestEntry.from_raw(raw)
    assert len(entry.images) == 1
    restored = ManifestEntry.from_line(entry.to_line())
    assert restored is not None
    assert len(restored.images) == 1
    assert restored.images[0].sha1 == asset.sha1
    assert restored.images[0].ocr_cache_path == asset.ocr_cache_path
    assert restored.images[0].source_url == asset.source_url


def test_manifest_entry_reads_legacy_lines_without_images() -> None:
    legacy = ManifestEntry.from_line('{"detail_url": "https://x/1", "html_path": "a.html"}')
    assert legacy is not None
    assert legacy.images == (), "老清单（无 images 字段）必须可读，保证向后兼容"


def test_clean_article_and_job_record_default_to_html() -> None:
    ref = ArticleRef(detail_url="https://example.edu.cn/a/kind")
    clean = CleanArticle(ref=ref, title="标题", text="正文")
    assert clean.source_kind == ContentKind.HTML.value
    assert JobRecord.empty(ref).content_kind == ContentKind.HTML.value


def test_ocr_counter_keys_are_registered() -> None:
    for key in ("images_fetched", "images_failed", "ocr_cached", "ocr_computed", "ocr_failed"):
        assert key in COUNTER_KEYS, f"{key} 必须登记在 COUNTER_KEYS，否则阶段无法统计"
    result = StageResult.start(Stage.EXTRACT)
    result.add("ocr_cached", 3).add("ocr_computed", 1)
    assert result.counters["ocr_cached"] == 3
    assert result.counters["ocr_computed"] == 1


def test_content_kind_is_declared_in_fields_yaml() -> None:
    declared = [
        f["name"]
        for f in yaml.safe_load((REPO_ROOT / FIELDS_RELATIVE_PATH).read_text(encoding="utf-8"))["fields"]
    ]
    assert "content_kind" in declared, "content_kind 必须是导出列，图片型数据才可统计与抽检"
    assert declared.index("content_kind") > declared.index("position"), "content_kind 属溯源/过程列，排在核心字段之后"


def test_http_response_ok_semantics() -> None:
    assert HttpResponse(url="u", status_code=200, text="<html>正文</html>").ok is True
    assert HttpResponse(url="u", status_code=200, content=b"\x89PNG\r\n").ok is True, (
        "二进制资源（图片）不带 text，也必须视为成功"
    )
    assert HttpResponse(url="u", status_code=200).ok is False, "空响应不算成功"
    assert HttpResponse(url="u", status_code=404, text="not found").ok is False
    assert HttpResponse(url="u", status_code=200, text="x", error="boom").ok is False
    assert HttpResponse(url="u", status_code=200, content=b"abcd").size == 4
    assert HttpResponse(url="u", status_code=200, text="abcd").size == 4


def test_http_response_records_redirect_target() -> None:
    """真实门户实测：未登录会被 302 到统一身份认证，落点必须记录下来。"""
    probe = "https://my.muc.edu.cn/page/11"
    cas = "https://ca.muc.edu.cn/zfca/login?service=x"

    assert HttpResponse(url=probe, status_code=200, text="<html>").redirected is False
    bounced = HttpResponse(url=probe, status_code=200, text="<html>", final_url=cas)
    assert bounced.redirected is True
    assert bounced.ok is True, "重定向后仍可能是 2xx —— 这正是「只看状态码会误判」的原因"


def test_login_status_carries_redirect_target() -> None:
    status = LoginStatus(
        authenticated=False,
        probe_url="https://my.muc.edu.cn/page/11",
        final_url="https://ca.muc.edu.cn/zfca/login?service=x",
        message="被重定向到统一身份认证",
    )
    assert status.final_url.endswith("service=x")

    with pytest.raises(LoginError):
        status.require()
