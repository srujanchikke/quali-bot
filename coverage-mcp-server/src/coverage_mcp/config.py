"""
Configuration loaded from environment variables.

Data source — pick one:
  MONGO_URI          — MongoDB connection string (recommended for production)
  COVERAGE_FILE_PATH — single JSON file (local dev / file mode)
  COVERAGE_DIR       — directory of per-build coverage_{tag}.json files

Transport:
  MCP_TRANSPORT=stdio   (default) | sse
  MCP_HOST=0.0.0.0
  MCP_PORT=8080
  MCP_API_KEY_HASH=<sha256 hex of bearer token>  (required for SSE)
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── MongoDB (production) ─────────────────────────────────────────────────
    MONGO_URI: str = os.environ.get("MONGO_URI", "")
    MONGO_DB:  str = os.environ.get("MONGO_DB", "coverage")

    # ── Local file fallback (dev / LLVM tools) ───────────────────────────────
    COVERAGE_FILE_PATH: str = os.environ.get("COVERAGE_FILE_PATH", "")
    COVERAGE_DIR:       str = os.environ.get("COVERAGE_DIR", "")

    # ── Auth ─────────────────────────────────────────────────────────────────
    MCP_API_KEY_HASH: str = os.environ.get("MCP_API_KEY_HASH", "")

    # ── Transport ────────────────────────────────────────────────────────────
    MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
    MCP_HOST:      str = os.environ.get("MCP_HOST", "0.0.0.0")
    MCP_PORT:      int = int(os.environ.get("MCP_PORT", "8080"))

    @classmethod
    def use_mongo(cls) -> bool:
        return bool(cls.MONGO_URI)

    @classmethod
    def validate(cls) -> None:
        if not cls.use_mongo() and not cls.COVERAGE_FILE_PATH and not cls.COVERAGE_DIR:
            raise RuntimeError(
                "Set MONGO_URI (production) or COVERAGE_FILE_PATH / COVERAGE_DIR (local dev)."
            )
        if cls.MCP_TRANSPORT == "sse" and not cls.MCP_API_KEY_HASH:
            raise RuntimeError(
                "MCP_API_KEY_HASH must be set for SSE transport. Run: python3 keygen.py"
            )


config = Config()
