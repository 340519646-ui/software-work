"""项目总入口（备用）：等价于 ``python -m src.pipeline.run``。

保留本文件是为了让 ``python -m src.main --stage fetch`` 也能工作，
内部直接委托给 CLI，**不重复实现任何业务逻辑**。

用法::

    python -m src.main --stage fetch
    python -m src.main --stage extract
    python -m src.main --stage export
"""

from __future__ import annotations

import sys
from typing import Optional, Sequence

from src.pipeline.run import main as run_main


def main(argv: Optional[Sequence[str]] = None) -> int:
    """把全部参数原样转交 CLI 层（退出码语义与 run.py 一致）。"""
    return run_main(sys.argv[1:] if argv is None else list(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
