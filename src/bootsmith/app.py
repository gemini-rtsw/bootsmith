from __future__ import annotations

import time
from dataclasses import asdict

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

from . import profiles as profiles_mod
from .session import SessionManager


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["sessions"] = SessionManager()

    @app.get("/")
    def index():
        return render_template("index.html", profiles=profiles_mod.list_profiles())

    @app.get("/profiles")
    def list_profiles_route():
        return render_template("_profile_list.html", profiles=profiles_mod.list_profiles())

    @app.post("/profiles")
    def create_profile():
        name = (request.form.get("name") or "").strip()
        host = (request.form.get("wti_host") or "").strip()
        try:
            port = int(request.form.get("wti_port") or "")
        except ValueError:
            return _error("wti_port must be an integer"), 400
        loader_hint = request.form.get("loader_hint", "auto")
        if not name or not host:
            return _error("name and wti_host are required"), 400
        if loader_hint not in {"auto", "ppcbug", "vxworks"}:
            return _error("loader_hint must be auto, ppcbug, or vxworks"), 400
        if profiles_mod.get_profile(name) is not None:
            return _error(f"profile {name!r} already exists"), 409
        profile = profiles_mod.Profile(
            name=name, wti_host=host, wti_port=port, loader_hint=loader_hint
        )
        profiles_mod.save_profile(profile)
        return render_template("_profile_list.html", profiles=profiles_mod.list_profiles())

    @app.post("/profiles/<name>/delete")
    def delete_profile_route(name: str):
        profiles_mod.delete_profile(name)
        return render_template("_profile_list.html", profiles=profiles_mod.list_profiles())

    @app.post("/session/open")
    def session_open():
        name = (request.form.get("name") or "").strip()
        profile = profiles_mod.get_profile(name)
        if profile is None:
            return _error(f"no such profile: {name!r}"), 404
        sessions: SessionManager = app.config["sessions"]
        try:
            sessions.open(profile)
        except Exception as e:
            return _error(str(e)), 400
        return render_template("_session.html", profile=profile)

    @app.post("/session/close")
    def session_close():
        sessions: SessionManager = app.config["sessions"]
        sessions.close()
        return render_template("_profile_list.html", profiles=profiles_mod.list_profiles())

    @app.get("/session/status")
    def session_status():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return jsonify({"open": False})
        ws = sess.watcher.status()
        return jsonify(
            {
                "open": True,
                "profile": sess.profile.name,
                "state": ws.state,
                "loader": ws.loader,
                "abort_chars_sent": ws.abort_chars_sent,
                "notes": ws.notes,
                "transport": asdict(sess.transport.status()),
            }
        )

    @app.post("/session/rearm")
    def session_rearm():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        sess.watcher.rearm()
        return ("", 204)

    @app.post("/session/force-prompt")
    def session_force_prompt():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        # Send a CR to make the board print its prompt, then let the watcher
        # match it from the resulting bytes.
        sess.transport.write(b"\r")
        # Give the board a moment to respond before we sample the buffer.
        import time as _t

        _t.sleep(0.25)
        sess.watcher.force_prompt()
        return ("", 204)

    @app.get("/session/stream")
    def session_stream():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        return Response(
            stream_with_context(_sse(sess)),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/session/send")
    def session_send():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        raw = request.form.get("data", "")
        # Allow \r, \n, and literal escape "\\r" / "\\n" for one-shot CR / LF sends.
        data = raw.encode("utf-8").replace(b"\\r", b"\r").replace(b"\\n", b"\n")
        sess.transport.write(data)
        return ("", 204)

    return app


def _error(msg: str) -> Response:
    return Response(
        f'<div class="error">{msg}</div>',
        status=400,
        content_type="text/html; charset=utf-8",
    )


def _sse(sess):
    """Server-sent events stream of serial bytes for the live view.

    Sends an initial 'snapshot' event with the ring-buffer history, then
    'chunk' events as new bytes arrive. Keepalive comments every ~15s so
    proxies don't kill the connection.
    """
    transport = sess.transport
    snapshot = transport.snapshot()
    yield f"event: snapshot\ndata: {_b64(snapshot)}\n\n"
    q = transport.subscribe()
    last_keepalive = time.time()
    try:
        while True:
            if q:
                chunk = q.popleft()
                yield f"event: chunk\ndata: {_b64(chunk)}\n\n"
            else:
                now = time.time()
                if now - last_keepalive > 15:
                    yield ": keepalive\n\n"
                    last_keepalive = now
                time.sleep(0.05)
            if not transport.status().connected:
                yield "event: closed\ndata: \n\n"
                return
    finally:
        transport.unsubscribe(q)


def _b64(b: bytes) -> str:
    import base64

    return base64.b64encode(b).decode("ascii")
