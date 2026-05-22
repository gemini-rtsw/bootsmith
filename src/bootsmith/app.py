from __future__ import annotations

import threading
import time
from dataclasses import asdict

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_sock import Sock

from . import ppcbug as ppcbug_mod
from . import profiles as profiles_mod
from . import schemas as schemas_mod
from . import vxworks as vxworks_mod
from .session import SessionManager


def _driver_for(loader: str):
    """Return the (write_params, read_params) module-level functions for a
    loader, or None if we don't have a driver yet for it.
    """
    if loader == "vxworks":
        return vxworks_mod
    if loader == "ppcbug":
        return ppcbug_mod
    return None


VALID_LOADERS = set(schemas_mod.LOADER_LABELS.keys())


def _render_profile_list():
    return render_template(
        "_profile_list.html",
        profiles=profiles_mod.list_profiles(),
        schemas=schemas_mod.SCHEMAS,
        loader_labels=schemas_mod.LOADER_LABELS,
    )


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["sessions"] = SessionManager()
    # Serialize push/read-params operations so two concurrent clicks
    # never run two _walk() calls against the same transport at once
    # (they would fight for queue subscribers and produce interleaved
    # nonsense).
    app.config["push_lock"] = threading.Lock()
    sock = Sock(app)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            profiles=profiles_mod.list_profiles(),
            schemas=schemas_mod.SCHEMAS,
            loader_labels=schemas_mod.LOADER_LABELS,
        )

    @app.get("/profiles")
    def list_profiles_route():
        return _render_profile_list()

    @app.post("/profiles")
    def create_profile():
        name = (request.form.get("name") or "").strip()
        host = (request.form.get("wti_host") or "").strip()
        try:
            port = int(request.form.get("wti_port") or "")
        except ValueError:
            return _error("wti_port must be an integer"), 400
        loader_hint = (request.form.get("loader_hint") or "").strip()
        if not name or not host:
            return _error("name and wti_host are required"), 400
        if loader_hint not in VALID_LOADERS:
            return _error(
                f"loader is required; pick one of {sorted(VALID_LOADERS)}"
            ), 400
        if profiles_mod.get_profile(name) is not None:
            return _error(f"profile {name!r} already exists"), 409
        profile = profiles_mod.Profile(
            name=name,
            wti_host=host,
            wti_port=port,
            loader_hint=loader_hint,
            boot_params=_collect_boot_params(request.form, loader_hint),
        )
        profiles_mod.save_profile(profile)
        return _render_profile_list()

    @app.post("/profiles/<name>/delete")
    def delete_profile_route(name: str):
        profiles_mod.delete_profile(name)
        return _render_profile_list()

    @app.post("/profiles/<name>/update")
    def update_profile_route(name: str):
        existing = profiles_mod.get_profile(name)
        if existing is None:
            return _error(f"no such profile: {name!r}"), 404
        host = (request.form.get("wti_host") or existing.wti_host).strip()
        try:
            port = int(request.form.get("wti_port") or existing.wti_port)
        except ValueError:
            return _error("wti_port must be an integer"), 400
        loader_hint = (request.form.get("loader_hint") or existing.loader_hint).strip()
        if loader_hint not in VALID_LOADERS:
            return _error(
                f"loader is required; pick one of {sorted(VALID_LOADERS)}"
            ), 400
        if not host:
            return _error("wti_host cannot be empty"), 400
        loader_changed = loader_hint != existing.loader_hint
        existing.wti_host = host
        existing.wti_port = port
        existing.loader_hint = loader_hint
        # Overwrite boot_params if the form submitted any param_* field,
        # or if the loader changed (old fields don't apply to new loader).
        new_params = _collect_boot_params(request.form, loader_hint)
        has_param_fields = any(k.startswith("param_") for k in request.form.keys())
        if has_param_fields or loader_changed:
            existing.boot_params = new_params

        # Handle rename if a new_name was submitted and it differs.
        new_name = (request.form.get("new_name") or "").strip()
        if new_name and new_name != name:
            try:
                profiles_mod.save_profile(existing)  # save current state first
                profiles_mod.rename_profile(name, new_name)
                existing.name = new_name
            except FileExistsError:
                return _error(f"profile {new_name!r} already exists"), 409
            except Exception as e:
                return _error(f"rename failed: {e}"), 400
        else:
            profiles_mod.save_profile(existing)
        # If the edit was triggered from inside an active session, return
        # the params panel so we don't blow away the session view.
        if request.headers.get("X-Return") == "params":
            sessions: SessionManager = app.config["sessions"]
            sess = sessions.current()
            if sess is not None:
                sess.profile = existing
            fields = schemas_mod.fields_for(existing.loader_hint)
            return render_template(
                "_params_push.html",
                profile=existing,
                fields=fields,
            )
        return _render_profile_list()

    @app.post("/session/open")
    def session_open():
        import sys as _sys

        name = (request.form.get("name") or "").strip()
        profile = profiles_mod.get_profile(name)
        if profile is None:
            return _error(f"no such profile: {name!r}"), 404
        sessions: SessionManager = app.config["sessions"]
        try:
            sessions.open(profile)
        except Exception as e:
            print(
                f"[session/open] failed for {name!r} -> "
                f"{profile.wti_host}:{profile.wti_port}: {type(e).__name__}: {e}",
                file=_sys.stderr,
                flush=True,
            )
            return _error(str(e)), 400
        return render_template("_session.html", profile=profile)

    @app.post("/session/close")
    def session_close():
        sessions: SessionManager = app.config["sessions"]
        sessions.close()
        return _render_profile_list()

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
        try:
            sess.transport.write(b"\r")
        except ConnectionError as e:
            return _error(f"WTI session is dead: {e}. Click Reconnect."), 502
        import time as _t

        _t.sleep(0.25)
        sess.watcher.force_prompt()
        return ("", 204)

    @app.post("/session/reconnect")
    def session_reconnect():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        try:
            sess.transport.reopen()
        except Exception as e:
            return _error(f"reconnect failed: {e}"), 502
        # Restart the watcher subscription cleanly.
        sess.watcher.stop()
        sess.watcher.start()
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

    @sock.route("/ws/terminal")
    def ws_terminal(ws):
        """WebSocket: bidirectional terminal pipe."""
        import sys as _sys
        import time as _t

        ws_id = id(ws)
        print(f"[ws {ws_id}] OPENED", file=_sys.stderr, flush=True)

        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            print(f"[ws {ws_id}] no session; sending ERR and closing", file=_sys.stderr, flush=True)
            try:
                ws.send("ERR no session open")
            except Exception:
                pass
            return

        transport = sess.transport
        q = transport.subscribe(seed_history=False)
        print(f"[ws {ws_id}] subscribed to {transport.host}:{transport.port}", file=_sys.stderr, flush=True)

        try:
            snap = transport.snapshot()
            CHUNK = 4096
            for i in range(0, len(snap), CHUNK):
                ws.send(bytes(snap[i : i + CHUNK]))
            print(f"[ws {ws_id}] sent snapshot ({len(snap)}B)", file=_sys.stderr, flush=True)
        except Exception as _e:
            print(f"[ws {ws_id}] snapshot send failed: {_e}", file=_sys.stderr, flush=True)
            transport.unsubscribe(q)
            return

        bytes_sent = 0
        bytes_recvd = 0
        last_log = _t.time()
        exit_reason = "unknown"
        try:
            while True:
                # Periodic alive-log every 10s so we can see hung-but-alive vs gone.
                now = _t.time()
                if now - last_log > 10:
                    print(
                        f"[ws {ws_id}] alive: connected={getattr(ws, 'connected', '?')} "
                        f"sent={bytes_sent}B recvd={bytes_recvd}B q={len(q)}",
                        file=_sys.stderr, flush=True,
                    )
                    last_log = now

                if not getattr(ws, "connected", True):
                    exit_reason = "ws.connected is False"
                    return

                # Drain board→browser. Coalesce everything currently in
                # the queue into a single ws.send to avoid pummeling the
                # browser with hundreds of tiny WS frames during a push
                # (each frame triggers onmessage + DOM append + reflow,
                # which can starve click/keydown event delivery).
                if q:
                    merged = bytearray()
                    while q:
                        merged.extend(q.popleft())
                    try:
                        ws.send(bytes(merged))
                        bytes_sent += len(merged)
                    except Exception as e:
                        exit_reason = f"send failed: {e}"
                        return

                # Non-blocking receive for browser→board.
                try:
                    msg = ws.receive(timeout=0.05)
                except Exception as e:
                    exit_reason = f"receive failed: {e}"
                    return
                if msg is None:
                    if not getattr(ws, "connected", True):
                        exit_reason = "receive None + ws disconnected"
                        return
                    if not transport.status().connected:
                        exit_reason = "transport disconnected"
                        try:
                            ws.send("EVT disconnected")
                        except Exception:
                            pass
                        return
                    continue
                if isinstance(msg, str):
                    if msg == "ping":
                        continue
                    data = msg.encode("utf-8", errors="replace")
                else:
                    data = msg
                if not data:
                    continue
                bytes_recvd += len(data)
                try:
                    transport.write(data)
                except Exception as e:
                    exit_reason = f"transport write failed: {e}"
                    try:
                        ws.send(f"ERR write failed: {e}")
                    except Exception:
                        pass
                    return
        finally:
            transport.unsubscribe(q)
            print(
                f"[ws {ws_id}] CLOSED reason={exit_reason} sent={bytes_sent}B recvd={bytes_recvd}B",
                file=_sys.stderr, flush=True,
            )

    @app.get("/params")
    def params_panel():
        """Show the saved profile params with a 'Push to board' button."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        # Pull a fresh profile from disk so edits made via /params/edit
        # show up here right away.
        profile = profiles_mod.get_profile(sess.profile.name) or sess.profile
        sess.profile = profile
        fields = schemas_mod.fields_for(profile.loader_hint)
        return render_template(
            "_params_push.html",
            profile=profile,
            fields=fields,
        )

    @app.get("/params/edit")
    def params_edit():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        profile = profiles_mod.get_profile(sess.profile.name) or sess.profile
        sess.profile = profile
        fields = schemas_mod.fields_for(profile.loader_hint)
        return render_template(
            "_params_edit.html",
            profile=profile,
            fields=fields,
        )

    @app.post("/params/push")
    def params_push():
        """Push the saved profile's boot_params to the board, then verify."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _error(f"not at a loader prompt (state={ws.state})"), 409
        driver = _driver_for(ws.loader or "")
        if driver is None:
            return _error(f"no driver for loader {ws.loader!r}"), 400
        values = dict(sess.profile.boot_params)
        lock: threading.Lock = app.config["push_lock"]
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409
        try:
            driver.write_params(sess.transport, values)
            verify = driver.read_params(sess.transport)
        except ConnectionError as e:
            return _error(f"WTI session is dead during push: {e}. Reconnect and retry."), 502
        finally:
            lock.release()
        fields = schemas_mod.fields_for(ws.loader)
        diff = []
        for _label, key in fields:
            want = values.get(key, "")
            got = verify.params.get(key, "")
            if not want:
                continue
            if want == ".":
                # We asked the board to clear this field. Success means the
                # field is absent from readback (or empty).
                if got:
                    diff.append({"key": key, "want": "(cleared)", "got": got})
                continue
            if want != got:
                diff.append({"key": key, "want": want, "got": got})
        return render_template(
            "_params_verify.html",
            profile=sess.profile,
            fields=fields,
            current=verify.params,
            wrote=values,
            diff=diff,
        )

    @app.post("/params/boot")
    def params_boot():
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        loader = sess.watcher.status().loader or sess.profile.loader_hint
        driver = _driver_for(loader)
        if driver is not None:
            try:
                driver.boot(sess.transport)
            except ConnectionError as e:
                return _error(f"WTI session is dead during boot: {e}."), 502
        # Stay connected so the user can watch the boot scroll past in the
        # live serial pane. Render a small status panel that replaces the
        # params section but keeps the session open.
        return render_template(
            "_params_booting.html",
            profile=sess.profile,
            fields=schemas_mod.fields_for(sess.profile.loader_hint),
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
        try:
            sess.transport.write(data)
        except ConnectionError as e:
            return _error(f"WTI session is dead: {e}"), 502
        return ("", 204)

    @app.post("/session/key")
    def session_key():
        """Send raw bytes from the terminal input.

        The browser posts a base64-encoded byte string in `b`. This is the
        path used by the interactive terminal box; one POST per keystroke or
        per small batch of bytes (the browser may coalesce fast typing).
        """
        import base64

        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        b64 = request.form.get("b", "")
        try:
            data = base64.b64decode(b64)
        except Exception:
            return _error("bad base64"), 400
        if data:
            try:
                sess.transport.write(data)
            except ConnectionError as e:
                return _error(f"WTI session is dead: {e}"), 502
        return ("", 204)

    return app


def _collect_boot_params(form, loader: str) -> dict[str, str]:
    """Pull `param_<key>` form fields into a {key: value} dict.

    Only keys that belong to the chosen loader's schema are kept, so
    switching loader doesn't bring along stale fields from the other.
    Empty values are dropped.
    """
    out: dict[str, str] = {}
    for _label, key in schemas_mod.fields_for(loader):
        v = form.get(f"param_{key}")
        if v is None:
            continue
        v = v.strip()
        if v:
            out[key] = v
    return out


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
    import sys

    transport = sess.transport
    print(
        f"[sse] subscriber attached for {sess.profile.name} ({transport.host}:{transport.port})",
        file=sys.stderr,
        flush=True,
    )
    snapshot = transport.snapshot()
    yield f"event: snapshot\ndata: {_b64(snapshot)}\n\n"
    q = transport.subscribe()
    last_keepalive = time.time()
    chunks_sent = 0
    try:
        while True:
            if q:
                chunk = q.popleft()
                chunks_sent += 1
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
        print(
            f"[sse] subscriber detached after {chunks_sent} chunks",
            file=sys.stderr,
            flush=True,
        )


def _b64(b: bytes) -> str:
    import base64

    return base64.b64encode(b).decode("ascii")
