"""把随包携带的运行时快照放进导入路径。

本项目自带引擎的取数与判据快照（`runtime/`），不读取任何外部目录：
整个项目拷到哪里都能单独运行，运行时只看本包内的这一份。
"""
from __future__ import annotations

import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "runtime"
ENGINE_ROOT = RUNTIME
SCRIPTS = RUNTIME / "scripts"

# 运行时是只读快照：别把 .pyc 落进包目录，分发树才能保持逐字节稳定。
sys.dont_write_bytecode = True

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from . import tax_service  # noqa: E402

tax_service.configure(ENGINE_ROOT)
