"""Export the current working tree as an isolated overlay for the P-016 layout.

Existing P-016 entrypoints, packages, environment files and voice code are never
included as replacement files. All exported implementations use parrotgo_chat
subpackages. Run with Python 3.12; no third-party dependencies are required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = "src.modules.core.parrotgo_chat"
LLM = "src.modules.llm.parrotgo_chat"
TOOL = "src.modules.tool.parrotgo_chat"
SERVICE = "src.services.parrotgo_chat"
DOMAIN = "src.domain.parrotgo_chat"
MODEL = "src.models.parrotgo_chat"
API = "src.api.parrotgo_chat"
DOCS = "src/parrotgo/parrotgo_chat"
HELPERS = "scripts/parrotgo_chat"
FRONTEND = "frontend/parrotgo_chat"
LLM_ADAPTERS = {
    "extractor", "extraction_router", "groq_rate_limit", "rate_limit",
    "nlu_fixture", "nlu_gemini", "nlu_groq", "nlu_openai", "turn_fixture",
}
SERVICE_ADAPTERS = {"crm_sqlite", "knowledge_base"}
SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "dist", ".git"}


def source_files(directory: Path):
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if (path.is_file() and not SKIP_DIRS.intersection(relative.parts)
                and path.suffix not in {".pyc", ".pyo"}
                and (not path.name.startswith(".env") or path.name == ".env.example")):
            yield path


def module_destination(relative: Path) -> str:
    parts = relative.parts
    if parts[0] == "adapters":
        name = relative.stem
        if name == "__init__":
            return f"{LLM}.__init__"
        package = LLM if name in LLM_ADAPTERS else (
            SERVICE if name in SERVICE_ADAPTERS else TOOL
        )
        return f"{package}.{name}"
    if parts[0] == "contracts":
        return f"{MODEL}.{relative.stem}"
    if parts[0] == "domain":
        return f"{DOMAIN}.{relative.stem}"
    if parts[0] == "graph":
        return f"{CORE}.graph" if relative.stem == "builder" else f"{CORE}.__init__"
    if parts[0] == "workers":
        return f"{CORE}.coordinator" if relative.stem == "coordinator" else ""
    if parts[0] == "evaluation":
        return "eval.parrotgo_chat." + relative.stem
    if parts[0] == "prompts":
        return f"{LLM}.prompts.{relative.stem}"
    if parts[0] == "persistence":
        return f"{SERVICE}.persistence.{relative.stem}"
    return {
        "config": f"{SERVICE}.config", "api_store": f"{SERVICE}.api_store",
        "main": f"{API}.routes", "runtime": f"{CORE}.runtime",
        "text": f"{CORE}.text", "__init__": "",
    }[relative.stem]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "exports/parrotgo-p016-source.zip")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--frontend-dist", type=Path, default=ROOT / "src/frontend/dist")
    args = parser.parse_args()
    stage = args.stage or ROOT / ".cache/p016-export" / datetime.now().strftime("%Y%m%d-%H%M%S")
    stage = stage.resolve()
    stage.relative_to(ROOT)  # Never stage outside the explicitly authorized workspace.
    if stage.exists():
        raise SystemExit(f"Staging directory already exists: {stage}")
    stage.mkdir(parents=True)
    records = []

    def put(name: str, content: str | bytes, source: Path | None = None):
        path = stage / name
        path.resolve().relative_to(stage)
        if path.exists():
            raise ValueError(f"Duplicate output: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(payload)
        record = {"path": name, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        if source:
            record.update(source=source.relative_to(ROOT).as_posix(),
                          source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        records.append(record)

    app_root = ROOT / "src/backend/app"
    mappings = {}
    for source in source_files(app_root):
        if source.suffix == ".py":
            relative = source.relative_to(app_root)
            destination = module_destination(relative)
            if destination:
                original = "app." + ".".join(relative.with_suffix("").parts)
                if relative.stem == "__init__":
                    original = original.removesuffix(".__init__")
                mappings[original] = destination.removesuffix(".__init__")
    mappings.update({"app.prompts": f"{LLM}.prompts", "app.graph": f"{CORE}.graph"})
    module_pattern = re.compile(r"\b(?:" + "|".join(
        re.escape(key) for key in sorted(mappings, key=len, reverse=True)
    ) + r")\b")

    def transform(source: Path, content: str) -> str:
        # Replace only known module paths, including importlib resource strings.
        content = module_pattern.sub(lambda match: mappings[match[0]], content)
        content = content.replace("from app import config", f"from {SERVICE} import config")
        content = content.replace("from app import runtime", f"from {CORE} import runtime")
        content = content.replace("from app.adapters import", f"from {LLM} import")
        content = content.replace("from tests.test_", "from parrotgo_chat.test_")
        content = re.sub(r"\bfrom (test_\w+) import", r"from parrotgo_chat.\1 import", content)
        if source.name == "config.py":
            content = content.replace("ROOT = Path(__file__).resolve().parents[1]",
                                      "ROOT = Path(__file__).resolve().parents[3]")
            content = content.replace('load_dotenv(ROOT / ".env", override=False)\n'
                                      '        load_dotenv(ROOT.parents[1] / ".env", override=False)',
                                      'load_dotenv(ROOT / ".env.parrotgo-chat", override=False)')
            content = content.replace('"data/', '"data/parrotgo_chat/')
        if source.name == "main.py" and source.parent == app_root:
            content = content.replace('ROOT.parent / "frontend/dist"',
                                      'ROOT / "frontend/parrotgo_chat/dist"')
            content = content.replace("npm --prefix src/frontend", "npm --prefix frontend/parrotgo_chat")
        fixture_base = 'Path(__file__).resolve().parents[1] / "fixtures'
        if fixture_base in content:
            # Cross-layer fixture reads now share one explicit resource root.
            content = content.replace(fixture_base, 'FIXTURE_DIR / "')
            content = content.replace('FIXTURE_DIR / "/', 'FIXTURE_DIR / "')
            content = content.replace('FIXTURE_DIR / "" / ', 'FIXTURE_DIR / ')
            content = content.replace('FIXTURE_DIR = FIXTURE_DIR / ""', '')
            content = content.replace('ROOT = FIXTURE_DIR / ""', 'ROOT = FIXTURE_DIR')
            marker = "from pathlib import Path\n"
            content = content.replace(marker, marker + f"\nfrom {SERVICE}.paths import FIXTURE_DIR\n", 1)
        if source.name == "mega_poi.py":
            content = content.replace('Path(__file__).resolve().parents[1] /\n'
                                      '                               "fixtures/mega_pois.json"',
                                      'FIXTURE_DIR / "mega_pois.json"')
            content = content.replace("from pathlib import Path\n",
                                      f"from pathlib import Path\n\nfrom {SERVICE}.paths import FIXTURE_DIR\n")
        if source.parent.name == "examples":
            if "sys.path.insert" not in content:
                bootstrap = ""
                if not re.search(r"(?m)^import sys$", content):
                    bootstrap += "import sys\n"
                if not re.search(r"(?m)^from pathlib import Path$", content):
                    bootstrap += "from pathlib import Path\n"
                bootstrap += "sys.path.insert(0, str(Path(__file__).resolve().parents[3]))\n\n"
                content = re.sub(r"(?m)^from src\.", lambda match: bootstrap + match[0], content, count=1)
            else:
                content = content.replace("Path(__file__).resolve().parents[1]",
                                          "Path(__file__).resolve().parents[3]")
            content = content.replace('ROOT.parent / ".cache/live-booking"', 'ROOT / ".cache/parrotgo_chat/live-booking"')
            content = content.replace('ROOT / "evaluation/', 'ROOT / "eval/parrotgo_chat/results/')
            content = content.replace(' / "evaluation/ver2/', ' / "eval/parrotgo_chat/results/ver2/')
            content = content.replace("src/backend/.env", ".env.parrotgo-chat")
        if source.name == "test_worker_recovery.py":
            content = content.replace("Path(__file__).resolve().parents[1]",
                                      "Path(__file__).resolve().parents[2]")
        return content

    for source in source_files(app_root):
        relative = source.relative_to(app_root)
        if source.suffix == ".py":
            destination = module_destination(relative)
            if not destination:
                continue
            # The original graph package initializer targets renamed graph.py.
            if relative.as_posix() == "graph/__init__.py":
                put(CORE.replace(".", "/") + "/__init__.py",
                    '"""Isolated P-016 chat orchestration."""\n', source)
                continue
            put(destination.replace(".", "/") + ".py",
                transform(source, source.read_text(encoding="utf-8")), source)
        elif relative.parts[0] == "fixtures":
            put(SERVICE.replace(".", "/") + "/" + relative.as_posix(), source.read_bytes(), source)
        elif relative.parts[0] == "prompts":
            put(LLM.replace(".", "/") + "/" + relative.as_posix(), source.read_bytes(), source)
        else:
            raise ValueError(f"Unmapped source: {relative}")

    put(SERVICE.replace(".", "/") + "/paths.py",
        '"""Read-only packaged fixture resources."""\nfrom pathlib import Path\n\n'
        'FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"\n')
    for package in (TOOL, SERVICE, API):
        put(package.replace(".", "/") + "/__init__.py", '"""Isolated ParrotGo chat module."""\n')
    put(CORE.replace(".", "/") + "/api.py",
        '"""Text entrypoint for the host Core/Speech adapter."""\n'
        f'from {CORE}.text import TextBot, main, main_async\n\n'
        '__all__ = ["TextBot", "core_func", "main", "main_async"]\n\n'
        'async def core_func(user_text: str, session_id: str = "default", **kwargs) -> str:\n'
        '    """First input requires customer_phone/customer_name; see integration guide."""\n'
        '    return await main_async(user_text, session_id=session_id, **kwargs)\n')

    for source in source_files(ROOT / "src/backend/tests"):
        if source.name == "test_setup_image.py":
            # Preserve the old-layout deployment test as a reference, not a live test.
            put(f"{DOCS}/original_deploy/test_setup_image.py", source.read_bytes(), source)
            continue
        content = transform(source, source.read_text(encoding="utf-8"))
        if source.name == "conftest.py":
            content = content.replace('import socket\n', 'import socket\nimport os\n')
            content = content.replace('    original_connect = socket.socket.connect',
                '    root = Path(__file__).resolve().parents[2]\n'
                '    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(root), str(root / "tests"))))\n'
                '    monkeypatch.delenv("GROQ_API_KEY", raising=False)\n'
                '    original_connect = socket.socket.connect')
            content += ('\n# Standalone suite: avoid the host repo and site-packages tests namespace.\n'
                        'import sys\nfrom pathlib import Path\n'
                        'root = Path(__file__).resolve().parents[2]\n'
                        'sys.path.insert(0, str(root))\n'
                        'sys.path.insert(0, str(root / "tests"))\n')
        put("tests/parrotgo_chat/" + source.name, content, source)
    put("tests/parrotgo_chat/__init__.py", '"""Portable chat regression tests."""\n')
    put(f"{HELPERS}/pytest.ini", '[pytest]\ntestpaths = ../../tests/parrotgo_chat\npythonpath = ../.. ../../tests\n')
    for source in source_files(ROOT / "src/backend/examples"):
        put(f"{HELPERS}/examples/{source.name}",
            transform(source, source.read_text(encoding="utf-8")), source)
    for source in source_files(ROOT / "src/backend/evaluation"):
        put("eval/parrotgo_chat/results/" + source.relative_to(ROOT / "src/backend/evaluation").as_posix(),
            source.read_bytes(), source)

    frontend_root = ROOT / "src/frontend"
    for source in source_files(frontend_root):
        relative = source.relative_to(frontend_root)
        if relative.parts[0] in {"test-results", "playwright-report", ".cache"}:
            continue
        content = source.read_bytes()
        if source.name == "playwright.config.ts":
            content = ("import { defineConfig } from '@playwright/test'\n"
                       "import path from 'node:path'\n"
                       "const root = path.resolve('../..')\n"
                       "const python = process.env.PARROTGO_CHAT_PYTHON || path.join(root, '.venv-parrotgo-chat', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')\n"
                       "export default defineConfig({\n"
                       "  testDir: './tests', timeout: 45000, fullyParallel: false, workers: 1,\n"
                       "  use: { baseURL: 'http://127.0.0.1:8001', headless: true },\n"
                       "  webServer: {\n"
                       f'    command: `"${{python}}" -m uvicorn {API}.routes:app --app-dir "${{root}}" --host 127.0.0.1 --port 8001 --workers 1`,\n'
                       "    url: 'http://127.0.0.1:8001/api/ready', reuseExistingServer: false,\n"
                       "    env: { APP_PROFILE: 'test', APP_SECRET: 'e2e-stable-secret',\n"
                       "      BUSINESS_DB_PATH: path.join(root, '.cache/parrotgo_chat/e2e/app.sqlite'),\n"
                       "      CHECKPOINT_DB_PATH: path.join(root, '.cache/parrotgo_chat/e2e/checkpoints.sqlite'),\n"
                       "      ALLOWED_ORIGINS: 'http://127.0.0.1:8001', AREA_ASSISTANCE_ENABLED: 'true' },\n"
                       "  },\n})\n").encode()
        elif source.name == "vite.config.ts":
            content = content.replace(b"127.0.0.1:8000", b"127.0.0.1:8001")
        elif source.name == "api.generated.ts":
            content = content.replace(b"python src/backend/scripts/generate_api_types.py",
                                      b"python scripts/parrotgo_chat/generate_api_types.py")
        put(f"{FRONTEND}/{relative.as_posix()}", content, source)
    dist = args.frontend_dist.resolve()
    if not (dist / "index.html").is_file():
        raise SystemExit("Build the existing frontend before packaging: npm.cmd --prefix src/frontend run build")
    for source in source_files(dist):
        put(f"{FRONTEND}/dist/{source.relative_to(dist).as_posix()}", source.read_bytes(), source)

    generator = ROOT / "src/backend/scripts/generate_api_types.py"
    content = transform(generator, generator.read_text(encoding="utf-8"))
    content = content.replace('BACKEND = Path(__file__).resolve().parents[1]',
                              'BACKEND = Path(__file__).resolve().parents[2]')
    content = content.replace('BACKEND.parent / "frontend/src/api.generated.ts"',
                              'BACKEND / "frontend/parrotgo_chat/src/api.generated.ts"')
    content = content.replace("src/backend/.venv/Scripts/python.exe src/backend/scripts/generate_api_types.py",
                              "python scripts/parrotgo_chat/generate_api_types.py")
    content = content.replace("src/backend/scripts/generate_api_types.py", f"{HELPERS}/generate_api_types.py")
    content = content.replace("src/frontend/src/api.generated.ts", f"{FRONTEND}/src/api.generated.ts")
    put(f"{HELPERS}/generate_api_types.py", content, generator)
    cli = ROOT / "src/backend/main.py"
    content = transform(cli, cli.read_text(encoding="utf-8"))
    console_setup = '    if hasattr(sys.stdout, "reconfigure"):\n        sys.stdout.reconfigure(encoding="utf-8")\n'
    content = content.replace(console_setup, "")
    content = content.replace("def cli():\n", "def cli():\n" + console_setup)
    put("run_parrotgo_chat.py", content, cli)
    for name, output in (("requirements.runtime.lock", "requirements.txt"),
                         ("requirements.lock", "requirements.dev.txt")):
        source = ROOT / "src/backend" / name
        put(f"{HELPERS}/{output}", source.read_bytes(), source)
    template = ROOT / "src/backend/.env.example"
    content = template.read_text(encoding="utf-8").replace("APP_PROFILE=chat_sandbox", "APP_PROFILE=fixture_demo")
    content = content.replace("=data/", "=data/parrotgo_chat/")
    content = content.replace("http://127.0.0.1:8000,http://localhost:8000", "http://127.0.0.1:8001,http://localhost:8001")
    put(".env.parrotgo-chat.example", content, template)
    put(f"{HELPERS}/init_env.py",
        '"""Initialize only the isolated chat configuration."""\n'
        'import secrets\nfrom pathlib import Path\n\n'
        'root = Path(__file__).resolve().parents[2]\n'
        'target = root / ".env.parrotgo-chat"\n'
        'if target.exists():\n    print("Keeping existing .env.parrotgo-chat")\n'
        'else:\n    text = (root / ".env.parrotgo-chat.example").read_text(encoding="utf-8")\n'
        '    text = text.replace("APP_SECRET=\\n", "APP_SECRET=" + secrets.token_urlsafe(48) + "\\n")\n'
        '    with target.open("x", encoding="utf-8", newline="\\n") as output:\n'
        '        output.write(text)\n'
        '    print("Created .env.parrotgo-chat (offline demo)")\n')
    put(f"{HELPERS}/run.ps1",
        'param([int]$Port = 8001)\n$ErrorActionPreference = "Stop"\n'
        '$chatRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path\n'
        'Set-Location -LiteralPath $chatRoot\n'
        '$chatPython = Join-Path $chatRoot ".venv-parrotgo-chat/Scripts/python.exe"\n'
        'if (-not (Test-Path -LiteralPath $chatPython)) {\n'
        '    py -3.12 -m venv .venv-parrotgo-chat\n'
        '    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 is required" }\n}\n'
        '& $chatPython -m pip install --require-hashes -r scripts/parrotgo_chat/requirements.txt\n'
        'if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }\n'
        '& $chatPython scripts/parrotgo_chat/init_env.py\n'
        'if ($LASTEXITCODE -ne 0) { throw "Configuration initialization failed" }\n'
        '$env:ALLOWED_ORIGINS = "http://127.0.0.1:$Port,http://localhost:$Port,http://127.0.0.1:5173,http://localhost:5173"\n'
        f'& $chatPython -m uvicorn {API}.routes:app --host 127.0.0.1 --port $Port --workers 1\n')
    put(f"{HELPERS}/run.sh",
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'cd "$(dirname "$0")/../.."\n'
        '[[ -x .venv-parrotgo-chat/bin/python ]] || python3.12 -m venv .venv-parrotgo-chat\n'
        '.venv-parrotgo-chat/bin/python -m pip install --require-hashes -r scripts/parrotgo_chat/requirements.txt\n'
        '.venv-parrotgo-chat/bin/python scripts/parrotgo_chat/init_env.py\n'
        'port="${1:-8001}"\n'
        'export ALLOWED_ORIGINS="http://127.0.0.1:$port,http://localhost:$port,http://127.0.0.1:5173,http://localhost:5173"\n'
        f'exec .venv-parrotgo-chat/bin/python -m uvicorn {API}.routes:app --host 127.0.0.1 --port "$port" --workers 1\n')

    for source in sorted(ROOT.glob("*.md")):
        put(f"{DOCS}/original_docs/{source.name}", source.read_bytes(), source)
    put(f"{DOCS}/original_docs/backend-README.md", (ROOT / "src/backend/README.md").read_bytes(), ROOT / "src/backend/README.md")
    for source in source_files(ROOT / "deploy"):
        if source.name == "package_p016.py" or source.suffix in {".zip", ".gz", ".tar"}:
            continue
        put(f"{DOCS}/original_deploy/{source.name}", source.read_bytes(), source)
    for name in ("Dockerfile", ".dockerignore", ".gitattributes", ".gitignore", "run-windows.bat"):
        source = ROOT / name
        put(f"{DOCS}/original_deploy/{name}", source.read_bytes(), source)
    for source in source_files(ROOT / ".github"):
        put(f"{DOCS}/original_deploy/github/{source.relative_to(ROOT / '.github').as_posix()}", source.read_bytes(), source)
    for source in source_files(ROOT / "src/scripts"):
        put(f"{DOCS}/original_deploy/src_scripts/{source.name}", source.read_bytes(), source)

    guide = f'''# Ghép mã chat vào repo P-016

Giải nén **nội dung ZIP trực tiếp vào thư mục gốc P-016** (nơi có `src/`,
`frontend/`, `scripts/`). ZIP không có lớp thư mục `parrotgo/` hay `P-016/` bao ngoài.
Mã được lấy từ working tree hiện tại, gồm cả thay đổi chưa commit.

## Bố trí theo CODEBASE_STRUCTURE.md

| Thành phần hiện tại | Đường dẫn trong P-016 |
| --- | --- |
| Graph, runtime, text adapter, coordinator | `src/modules/core/parrotgo_chat/` |
| Extractor, Groq/Gemini/OpenAI, quota, prompts | `src/modules/llm/parrotgo_chat/` |
| Map, quote cũ, booking sandbox, weather | `src/modules/tool/parrotgo_chat/` |
| Luật nghiệp vụ, xác nhận, địa chỉ, thời gian | `src/domain/parrotgo_chat/` |
| Pydantic contracts | `src/models/parrotgo_chat/` |
| Settings, inbox SQLite, CRM, KB, fixtures | `src/services/parrotgo_chat/` |
| HTTP API và app riêng | `src/api/parrotgo_chat/routes.py` |
| React/Vite, tests và bản giao diện đã build | `frontend/parrotgo_chat/` |
| Kiểm thử và đánh giá | `tests/parrotgo_chat/`, `eval/parrotgo_chat/` |
| Lệnh chạy và dependencies khóa hash | `scripts/parrotgo_chat/` |
| Tài liệu gốc và triển khai cũ để tham khảo | `src/parrotgo/parrotgo_chat/` |

Không thay `src/main.py`, `src/config.py`, `src/modules/*/api.py`, `frontend/index.html`,
`.env`, `requirements.txt`, `Dockerfile`, voice routes, LiveKit hoặc Speech của P-016.
Các parent package `__init__.py` của repo đích cũng không có trong ZIP.
Nếu đã từng ghép gói này, các file `parrotgo_chat` trùng tên sẽ được thay khi giải nén.
Manifest liệt kê từng đường dẫn và SHA-256. Chưa biết đường dẫn repo đích nên chưa
kiểm tra xung đột thực tế hoặc hợp đồng API/Core/Speech của repo đó.

## Chạy ngay sau khi giải nén

Yêu cầu Python 3.12 và tải dependencies lần đầu. Giao diện đã build sẵn, không
cần Node.js để chạy demo. Từ gốc P-016 trên Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/parrotgo_chat/run.ps1
```

Linux: `bash scripts/parrotgo_chat/run.sh`.
Mở `http://127.0.0.1:8001/`. Chọn cổng khác: `run.ps1 -Port 8002` hoặc `run.sh 8002`.
Môi trường Python riêng `.venv-parrotgo-chat`; cấu hình riêng `.env.parrotgo-chat`;
dữ liệu mới tự tạo trong `data/parrotgo_chat/`. Không chuyển secret hoặc SQLite cũ.
Mặc định `fixture_demo`, chạy thử offline không cần API key. Muốn gọi provider thật,
đổi `APP_PROFILE=chat_sandbox` rồi điền key trong `.env.parrotgo-chat`.
Không mở đồng thời nhiều runtime/process dùng cùng cặp database.

## Điểm nối với Core/Speech hiện có

Gói này thêm mã theo cây thư mục; giải nén không tự sửa luồng voice đang chạy.
Để gọi từ adapter hiện có, import entrypoint mới:

```python
from {CORE}.api import core_func

reply = await core_func(
    user_text, session_id,
    customer_phone="0901234567", customer_name="An",
)
```

Tên và SĐT bắt buộc ở lần đầu của phiên; các lượt sau có thể chỉ truyền text và
session_id. Với voice worker sống lâu, dùng `TextBot` như async context manager;
truyền `acknowledge_previous=False` cho `bot.ask` khi TTS chưa phát xong.
`core_func` trả chuỗi, có thêm kwargs riêng; không mặc định thay thế hàm Core cũ.
HTTP dùng `/api/...` như mã hiện tại, không tự chuyển thành `/api/v1/chat` của tài
liệu đích. STT/TTS, Speech, LiveKit và voice worker không có trong mã nguồn hiện tại.
Booking provider hiện tại vẫn là sandbox. Không tạo file giả cho chức năng thiếu.

CLI: `.venv-parrotgo-chat/Scripts/python.exe run_parrotgo_chat.py --phone 0901234567 --name An`.
Các lệnh CLI/HTTP mặc định đọc cấu hình chat riêng, không đọc `.env` của P-016.

## Phát triển và kiểm thử

```powershell
.venv-parrotgo-chat/Scripts/python.exe -m pip install --require-hashes -r scripts/parrotgo_chat/requirements.dev.txt
.venv-parrotgo-chat/Scripts/python.exe -m pytest -c scripts/parrotgo_chat/pytest.ini tests/parrotgo_chat -q
.venv-parrotgo-chat/Scripts/python.exe scripts/parrotgo_chat/generate_api_types.py --check
npm.cmd --prefix frontend/parrotgo_chat ci
npm.cmd --prefix frontend/parrotgo_chat run build
```

Vite proxy trỏ backend 8001. Playwright dùng Python riêng, hoặc biến
`PARROTGO_CHAT_PYTHON` để chỉ định Python 3.12 đã cài dependencies.
Tests module hiện có được chuyển import/đường dẫn cùng mã. Test triển khai VPS cũ
được giữ ở `original_deploy/test_setup_image.py` vì nó kiểm tra layout `src/backend`
cũ, không áp dụng cho gói ghép này. `original_docs` và `original_deploy` là bản gốc
để tra cứu, không phải lệnh triển khai cho layout mới.

## Quy tắc ứng xử

Đã đọc CODE_OF_CONDUCT.md: tôn trọng người đóng góp, góp ý vào code, giữ thông tin
riêng tư và báo cáo vi phạm kín. Bản gốc được giữ trong `original_docs/`.
'''
    put("docs/PARROTGO_CHAT_INTEGRATION.md", guide)
    put(f"{DOCS}/README.md", "Xem [hướng dẫn ghép và chạy](../../../docs/PARROTGO_CHAT_INTEGRATION.md).\n")
    for package in ("tests", "scripts", "eval", "frontend"):
        put(f"{package}/parrotgo_chat/.gitignore", "__pycache__/\n*.pyc\nnode_modules/\ntest-results/\nplaywright-report/\n")
    put("data/parrotgo_chat/.gitignore", "*\n!.gitignore\n")
    manifest = {
        "format": "parrotgo-p016-overlay-1", "archive_root": ".",
        "target": "Existing P-016 repository; isolated parrotgo_chat namespaces",
        "source": "Current working tree, including uncommitted changes",
        "excluded": [".git", ".venv", "node_modules", "local .env files", "secrets", "runtime databases", "caches"],
        "preserved_host_files": ["src/main.py", "src/config.py", ".env", "requirements.txt", "Dockerfile", "src/modules/*/api.py", "frontend/index.html"],
        "files": sorted(records, key=lambda record: record["path"]),
    }
    put("docs/PARROTGO_CHAT_MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    output = args.output.resolve()
    output.relative_to(ROOT)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                name = path.relative_to(stage).as_posix()
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError(f"Unsafe member: {name}")
                archive.write(path, name)
    with zipfile.ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError(f"ZIP CRC failure: {bad}")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(json.dumps({"archive": str(output), "stage": str(stage), "files": len(records),
                      "bytes": output.stat().st_size, "sha256": digest}))


if __name__ == "__main__":
    main()
