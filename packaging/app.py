#!/usr/bin/env python3
"""
app.py — native macOS wrapper for the Earditor review UI.

Starts the existing Flask review server on a local port in a background thread and
shows it in a native window via pywebview — so Earditor runs as a double-click .app
with no terminal. All of the review/verification logic is reused unchanged; this file
only adds the window shell.

    python3 packaging/app.py        # run the wrapped app in dev
    python3 packaging/setup_app.py py2app   # build Earditor.app (see PACKAGING.md)
"""

import os
import socket
import sys
import threading
import time

# Make the project root importable + the working directory, whether launched from
# the repo (python3 packaging/app.py) or from inside a py2app bundle.
ROOT = os.environ.get(
    "RESOURCEPATH", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import webview  # noqa: E402  (import after sys.path fix)

import db  # noqa: E402
from config import DB_PATH  # noqa: E402
from review import app, _free_port  # noqa: E402


def _serve(port):
    # use_reloader=False is required: the reloader spawns a second process, which
    # breaks the pywebview main thread. debug stays off in the shipped app.
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


def _wait_ready(port, timeout=10.0):
    """Block until the Flask server accepts connections (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    db.init_db(str(DB_PATH))
    port = _free_port(int(os.environ.get("PORT", "5001")))
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    if not _wait_ready(port):
        raise RuntimeError("The local review service did not start in time")
    webview.create_window(
        "Earditor — Review",
        f"http://127.0.0.1:{port}",
        width=1200, height=900, min_size=(760, 620),
    )
    webview.start()


if __name__ == "__main__":
    main()
