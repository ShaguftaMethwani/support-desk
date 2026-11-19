"""Write the API's OpenAPI schema to a file, for the frontend's generated types.

Run from backend/: uv run python -m scripts.export_openapi ../frontend/openapi.json
(the frontend's `npm run gen:api` does this). No server or database is needed.
"""

import json
import sys
from pathlib import Path

from app.main import create_app


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    out.write_text(json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
