"""`uv run python -m app` -> serve Naap on http://127.0.0.1:8777 (localhost only by default).

HOST and PORT override the bind address, e.g. HOST=0.0.0.0 in a container. Binding to anything
other than localhost exposes the app (and every uploaded drawing) to your network: it has no login.
"""
import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8777"))
    print(f"Naap (drawing -> MB) running at http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")
