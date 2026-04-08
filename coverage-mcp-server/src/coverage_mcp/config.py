"""
Configuration loaded from environment variables.

Single-file mode:
  COVERAGE_FILE_PATH=/data/index.json

Directory mode (multiple builds):
  COVERAGE_DIR=/data

Transport:
  MCP_TRANSPORT=stdio   (default) | sse
  MCP_HOST=0.0.0.0
  MCP_PORT=8080
  MCP_API_KEY_HASH=<sha256 hex of bearer token>  (required for SSE)
"""

import os


class Config:
    COVERAGE_FILE_PATH: str = os.environ.get("COVERAGE_FILE_PATH", "")
    COVERAGE_DIR: str = os.environ.get("COVERAGE_DIR", "")
    MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
    MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
    MCP_PORT: int = int(os.environ.get("MCP_PORT", "8080"))
    MCP_API_KEY_HASH: str = os.environ.get("MCP_API_KEY_HASH", "")

    def validate(self) -> None:
        if not self.COVERAGE_FILE_PATH and not self.COVERAGE_DIR:
            raise RuntimeError(
                "Set COVERAGE_FILE_PATH (single file) or COVERAGE_DIR (directory of builds)."
            )


config = Config()
