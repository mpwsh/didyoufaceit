"""Flask web server for faceit-finder."""

from __future__ import annotations
import os
import json
import traceback
import logging
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
from .api import FaceitClient
from .search import stream_search
from .cache import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
CORS(app)
init_db()


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/search")
def search():
    you      = (request.args.get("you")      or "").strip()
    opponent = (request.args.get("opponent") or "").strip()

    if not you or not opponent:
        return jsonify({"error": "Both nicknames are required"}), 400

    api_key = os.environ.get("FACEIT_API_KEY", "")
    if not api_key:
        return jsonify({"error": "FACEIT_API_KEY not set on server"}), 500

    log.info("SSE search: you=%r opponent=%r", you, opponent)

    def generate():
        try:
            with FaceitClient(api_key) as client:
                for event in stream_search(client, you, opponent):
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Stream failed:\n%s", tb)
            yield f"data: {json.dumps({'type': 'error', 'msg': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering if behind proxy
        },
    )


def run():
    port = int(os.environ.get("PORT", 5000))
    log.info("Starting faceit-finder on http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)