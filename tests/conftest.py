"""pytest 公共配置与共享夹具。

* 把项目根目录加入 import 路径（``pytest -v`` 与 ``python -m pytest`` 都能用）；
* 提供 ``cfg_root`` / ``cfg`` 夹具：在临时目录里生成一套最小可用配置，
  所有产物（数据库、CSV、归档）都落在 tmp_path，绝不污染项目 data/；
* 提供 ``fake_site`` 夹具：内存版门户（列表页 + 详情页 + 图片），
  让采集与编排能在**完全离线**的情况下被真实代码路径覆盖。
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import CONFIG_RELATIVE_PATH, FIELDS_RELATIVE_PATH, load_config  # noqa: E402
from src.contracts import HttpResponse  # noqa: E402

ALIASES_RELATIVE_PATH = "config/aliases.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

LIST_URL = "https://example.edu.cn/jobs"
DETAIL_TEXT = "https://example.edu.cn/info/2025/1001.htm"
DETAIL_IMAGE = "https://example.edu.cn/info/2025/1002.htm"
DETAIL_PORTAL = "https://portal.example.edu.cn/info/2024/0908.htm"
IMAGE_PNG = "https://example.edu.cn/__local/6/A1/2B/1234_ABCD.png"
IMAGE_JPG = "https://portal.example.edu.cn/__local/9/C3/44/5678_EFGH.jpg"

MUC_API_URL = "https://my.muc.edu.cn/comsys-portal-notice-web/getNoticeByPage"
MUC_DETAIL_TEMPLATE = (
    "https://my.muc.edu.cn/page/11#/print?notice_id={notice_id}&show_type=1&type=10"
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload" * 20
"""验收只需要「下载 → 内容寻址归档」链路，不需要可解码的真实图片（OCR 默认关闭）。"""

BASE_CONFIG: Dict[str, object] = {
    "fields_file": "config/fields.yaml",
    "portal": {
        "base_url": "https://example.edu.cn",
        "list_url": LIST_URL,
        "page_param": "page",
        "page_start": 1,
        "page_end": 1,
        "detail_link_selector": "div.news-list li.item a[href]",
    },
    "auth": {"method": "account", "session_file": "data/.session.json"},
    "request": {"interval_seconds": 2.0, "timeout": 20, "retries": 1},
    "extract": {
        "rule_first": True,
        "missing_placeholder": "未知",
        "manual_review_output": "data/processed/manual_review.csv",
        "raw_html_dir": "data/raw/html",
        "ocr_images_dir": "data/raw/images",
        "ocr_cache_dir": "data/raw/ocr",
        "lexicon_file": ALIASES_RELATIVE_PATH,
        "llm_trigger_below": 7,
    },
    "llm": {"enabled": False, "provider": "openai"},
    "ocr": {"enabled": False, "lang": "chi_sim+eng"},
    "storage": {"db_path": "data/employment.db", "batch_size": 2},
    "output": {"csv_path": "data/processed/jobs.csv", "xlsx_path": "data/processed/jobs.xlsx"},
    "sampling": {"review_rate": 0.1, "seed": 42},
    "logging": {"level": "INFO"},
}


def write_config(root: Path, config: Dict[str, object]) -> None:
    (root / CONFIG_RELATIVE_PATH).write_text(
        yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
    )


def patched_config(**sections: Dict[str, object]) -> Dict[str, object]:
    """返回覆盖了若干配置段的副本（供负例测试使用）。"""
    config = copy.deepcopy(BASE_CONFIG)
    for name, patch_values in sections.items():
        config[name] = {**config[name], **patch_values}  # type: ignore[dict-item]
    return config


@pytest.fixture()
def cfg_root(tmp_path: Path) -> Path:
    """最小可用项目配置目录（fields.yaml 与 aliases.yaml 用仓库里的真实文件）。"""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_ROOT / FIELDS_RELATIVE_PATH, tmp_path / FIELDS_RELATIVE_PATH)
    shutil.copy(REPO_ROOT / ALIASES_RELATIVE_PATH, tmp_path / ALIASES_RELATIVE_PATH)
    write_config(tmp_path, BASE_CONFIG)
    return tmp_path


@pytest.fixture()
def cfg(cfg_root: Path):
    """已加载并校验通过的 AppConfig（数据库、导出路径都指向 tmp_path）。"""
    return load_config(config_path=cfg_root / CONFIG_RELATIVE_PATH, project_root=cfg_root)


class FakeTransport:
    """实现 ``contracts.Transport`` 的内存版门户：不联网、可断言请求次数。"""

    def __init__(
        self,
        pages: Optional[Dict[str, str]] = None,
        images: Optional[Dict[str, bytes]] = None,
        json_routes: Optional[Dict[str, object]] = None,
    ) -> None:
        self.pages = dict(pages or {})
        self.images = dict(images or {})
        self.json_routes = dict(json_routes or {})
        self.requests: List[str] = []
        self.posts: List[Dict[str, object]] = []
        self.closed = False

    def post_json(self, url: str, payload, *, referer: str = "") -> HttpResponse:
        """模拟 JSON 列表接口。

        路由按**路径**匹配（真实服务端也如此：路径定路由、查询串定参数）——
        参数走查询串后，请求 URL 会带上 `?currentPage=2&type=10&…`，
        按完整 URL 精确匹配会全部落空。
        """
        self.requests.append(url)
        self.posts.append(dict(payload))

        base = url.split("?", 1)[0]
        body = self.json_routes.get(url)
        if body is None:
            body = self.json_routes.get(base)
        if body is None:
            for key, value in self.json_routes.items():
                if key.split("?", 1)[0] == base:
                    body = value
                    break
        if body is None:
            return HttpResponse(url=url, status_code=404, error="no json route")

        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        return HttpResponse(url=url, status_code=200, text=text, final_url=url)

    def get(self, url: str, *, referer: str = "", binary: bool = False) -> HttpResponse:
        self.requests.append(url)
        if binary:
            data = self.images.get(url)
            if data is None:
                return HttpResponse(url=url, status_code=404, error="image not found")
            return HttpResponse(url=url, status_code=200, content=data)
        html = self.pages.get(url)
        if html is None:
            return HttpResponse(url=url, status_code=404, error="page not found")
        return HttpResponse(url=url, status_code=200, text=html)

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_site() -> FakeTransport:
    """用 tests/fixtures 里的样本搭一个假门户：列表页 3 篇文章（第 4 条是重复链接）+ 2 张图。"""
    list_html = (FIXTURES / "list_page.html").read_text(encoding="utf-8")
    text_html = (FIXTURES / "article_text.html").read_text(encoding="utf-8")
    image_html = (FIXTURES / "article_image.html").read_text(encoding="utf-8")
    return FakeTransport(
        pages={
            LIST_URL: list_html,
            LIST_URL + "?page=1": list_html,
            DETAIL_TEXT: text_html,
            DETAIL_IMAGE: image_html,
            DETAIL_PORTAL: text_html,
        },
        images={IMAGE_PNG: PNG_BYTES, IMAGE_JPG: PNG_BYTES},
    )
