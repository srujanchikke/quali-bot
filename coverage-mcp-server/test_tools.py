"""
Manual tool tester for the coverage MCP server.

Usage:
    python3 test_tools.py                         # runs all examples
    python3 test_tools.py summarize_report
    python3 test_tools.py get_file_coverage stripe.rs
    python3 test_tools.py get_folder_coverage crates/hyperswitch_connectors
    python3 test_tools.py get_uncovered_functions --file stripe.rs
    python3 test_tools.py get_uncovered_functions --folder crates/hyperswitch_connectors
    python3 test_tools.py list_files
    python3 test_tools.py list_builds
"""

import json
import sys
import threading
import time
import urllib.request
import urllib.error
from queue import Queue

HOST = "http://localhost:9090"
API_KEY = "test_admin"
TAG = "local"          # single-file mode always uses this tag


def _sse_session() -> str:
    """Open SSE connection, return the session POST URL."""
    endpoint_q: Queue[str] = Queue()

    def _stream():
        req = urllib.request.Request(
            f"{HOST}/sse",
            headers={"Accept": "text/event-stream", "Authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(req) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    path = line.removeprefix("data:").strip()
                    endpoint_q.put(f"{HOST}{path}")
                    time.sleep(60)   # keep connection alive while test runs

    t = threading.Thread(target=_stream, daemon=True)
    t.start()
    return endpoint_q.get(timeout=5)


def call_tool(name: str, arguments: dict) -> str:
    session_url = _sse_session()
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()

    req = urllib.request.Request(
        session_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
            # MCP returns result in body or the SSE stream; for quick testing
            # the server echoes the result in the HTTP response for tool calls.
            return json.dumps(body, indent=2)
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()}"


def run(name: str, args: dict):
    print(f"\n{'='*60}")
    print(f"Tool: {name}  args: {args}")
    print('='*60)
    print(call_tool(name, args))


if __name__ == "__main__":
    argv = sys.argv[1:]
    tool = argv[0] if argv else None

    if not tool or tool == "list_builds":
        run("list_builds", {})

    if not tool or tool == "summarize_report":
        run("summarize_report", {"tag": TAG})

    if not tool or tool == "list_files":
        run("list_files", {"tag": TAG, "sort_by": "missed_functions", "limit": 20})

    if not tool or tool == "get_file_coverage":
        file_q = argv[1] if len(argv) > 1 else "stripe.rs"
        run("get_file_coverage", {"tag": TAG, "file": file_q})

    if not tool or tool == "get_folder_coverage":
        folder = argv[1] if len(argv) > 1 else "crates/hyperswitch_connectors"
        run("get_folder_coverage", {"tag": TAG, "folder": folder, "top_n": 10})

    if not tool or tool == "get_uncovered_functions":
        # parse --file / --folder flags
        file_f = folder_f = ""
        for i, a in enumerate(argv[1:], 1):
            if a == "--file" and i + 1 < len(argv):   file_f  = argv[i + 1]
            if a == "--folder" and i + 1 < len(argv): folder_f = argv[i + 1]
        if not file_f and not folder_f:
            file_f = "stripe.rs"
        run("get_uncovered_functions", {"tag": TAG, "file": file_f, "folder": folder_f, "limit": 30})
