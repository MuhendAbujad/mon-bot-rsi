"""
Minimal Flask HTTP server that runs in a background daemon thread.

Purpose: give UptimeRobot (or any uptime monitor) a URL to ping every
5 minutes so the Replit workspace never sleeps on the free plan.

Usage in bot.py:
    from keep_alive import start_keep_alive
    start_keep_alive()          # call once before app.run_polling()

UptimeRobot setup:
    Monitor type : HTTP(s)
    URL          : https://<your-replit-dev-domain>/ping
    Interval     : 5 minutes
"""

import logging
import os
import threading

from flask import Flask

logger = logging.getLogger(__name__)

_app = Flask(__name__)
_app.logger.disabled = True          # silence Flask's own access log
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@_app.route("/")
@_app.route("/ping")
def ping():
    return "OK", 200


@_app.route("/health")
def health():
    return {"status": "running", "bot": "Gold Trading Bot"}, 200


def start_keep_alive(port: int | None = None) -> None:
    """
    Start the Flask server in a daemon background thread.
    The thread dies automatically when the main process exits.
    """
    _port = port or int(os.environ.get("KEEP_ALIVE_PORT", 8000))

    def _run():
        try:
            _app.run(host="0.0.0.0", port=_port, debug=False, use_reloader=False)
        except Exception as exc:
            logger.warning(f"[KeepAlive] Server stopped: {exc}")

    thread = threading.Thread(target=_run, name="keep-alive", daemon=True)
    thread.start()
    logger.info(f"[KeepAlive] HTTP server started on port {_port} — ping /ping or /health")
