"""定位引擎并把它的 scripts/ 放进导入路径。

三个适配器项目各自保留这一小段：它们要能单独拷走、单独运行，不能靠
一个只在本机成立的包名去 import 引擎。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def find_engine_root() -> Path:
    candidates: list[Path] = []

    env = os.environ.get("TAX_ENGINE_ROOT", "").strip()
    if env:
        candidates.append(Path(env).expanduser().resolve())

    starts = [Path(__file__).resolve().parent, Path.cwd().resolve()]
    for start in starts:
        for base in (start, *start.parents):
            candidates.extend((base, base / "tax-analysis-skill"))

    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if (cand / "scripts" / "tax_search.py").is_file():
            return cand
    raise RuntimeError(
        "找不到 tax-analysis-skill：设置 TAX_ENGINE_ROOT 指向引擎根目录，"
        "或把本项目放在引擎同级目录"
    )


ENGINE_ROOT = find_engine_root()
SCRIPTS = ENGINE_ROOT / "scripts"

# 适配器对引擎只读不写：一旦 import 引擎脚本，解释器默认会往
# 引擎的 scripts/__pycache__ 落 .pyc，那是往引擎目录写文件。
sys.dont_write_bytecode = True

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from . import tax_service  # noqa: E402

tax_service.configure(ENGINE_ROOT)
