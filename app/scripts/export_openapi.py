"""Writes the current OpenAPI schema to ../docs/api/openapi.json, in the main repository.

Usage: uv run python -m app.scripts.export_openapi

This is a local, manual step — not a CI job. Each submodule's CI only checks out its own
repository, so it cannot write into a sibling repo's working tree. The developer runs this
after any change to a router's request/response shape, then commits docs/api/openapi.json
in the main repo (Task 18).
"""

import json
from pathlib import Path

from app.core.config import Settings
from app.main import create_app

_OUTPUT_PATH = Path(__file__).resolve().parents[2] / ".." / "docs" / "api" / "openapi.json"


def main() -> None:
    # Minimal settings only to mount the app enough to read the schema — no database
    # connection is opened on this path.
    app = create_app(
        settings=Settings(
            database_url="postgresql+asyncpg://placeholder/placeholder",
            jwt_secret_key="placeholder",
            qr_signing_private_key_hex="00" * 32,
        )
    )
    schema = app.openapi()

    output_path = _OUTPUT_PATH.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    output_path.write_text(content, encoding="utf-8")
    print(f"OpenAPI schema written to {output_path}")


if __name__ == "__main__":
    main()
