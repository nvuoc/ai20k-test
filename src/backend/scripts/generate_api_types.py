"""Generate dependency-free TypeScript aliases from this server's OpenAPI.

Run from either workspace root or backend:
    src/backend/.venv/Scripts/python.exe src/backend/scripts/generate_api_types.py
Use --check in validation to detect a stale committed frontend contract.
The app lifespan is never started, so no credentials, database or network are used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

OUTPUT = BACKEND.parent / "frontend/src/api.generated.ts"


def ts_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    for union in ("anyOf", "oneOf"):
        if union in schema:
            return " | ".join(ts_type(item) for item in schema[union])
    if "allOf" in schema:
        return " & ".join(ts_type(item) for item in schema["allOf"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(ts_type({**schema, "type":item}) for item in kind)
    if kind == "array":
        return "Array<" + ts_type(schema.get("items", {})) + ">"
    if kind == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        fields = [
            "  " + json.dumps(name) + ("" if name in required else "?") + ": " + ts_type(value) + ";"
            for name, value in props.items()
        ]
        additional = schema.get("additionalProperties", False)
        if isinstance(additional, dict):
            fields.append("  [key: string]: " + ts_type(additional) + ";")
        elif additional:
            fields.append("  [key: string]: unknown;")
        return "{\n" + "\n".join(fields) + "\n}"
    return {"string":"string", "integer":"number", "number":"number", "boolean":"boolean", "null":"null"}.get(kind, "unknown")


def generated_types() -> str:
    app = create_app(Settings(profile="test", secret="schema-generation-only"))
    schemas = app.openapi()["components"]["schemas"]
    lines = ["// Generated from FastAPI OpenAPI. Do not edit by hand.",
             "// Regenerate: python src/backend/scripts/generate_api_types.py", ""]
    lines.extend(f"export type {name} = {ts_type(schema)};\n" for name, schema in sorted(schemas.items()))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generated = generated_types()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != generated:
            raise SystemExit("Frontend API types are stale; regenerate api.generated.ts")
        print("OpenAPI frontend contract is current")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print("Generated src/frontend/src/api.generated.ts")


if __name__ == "__main__":
    main()
