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
        ppcbug_niot_fields=ppcbug_mod.NIOT_USER_FIELDS,
        ppcbug_env_fields=ppcbug_mod.ENV_USER_FIELDS,
    )


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["sessions"] = SessionManager()
    # Serialize push/read-params operations so two concurrent clicks
    # never run two _walk() calls against the same transport at once
    # (they would fight for queue subscribers and produce interleaved
    # nonsense).
    app.config["push_lock"] = threading.Lock()
    # In-memory state for the latest async push job. Single-session app,
    # so one slot is enough. See /params/push (queues a job) and
    # /params/push/status (polls it).
    app.config["push_job"] = {
        "id": 0,
        "state": "idle",        # idle | running | done | error
        "started_at": 0.0,
        "finished_at": 0.0,
        "verify_html": "",      # rendered _params_verify.html when done
        "error": "",
    }
    app.config["push_job_lock"] = threading.Lock()
    # Same single-slot pattern for the one-shot CNFG;M VPD repair.
    app.config["cnfg_job"] = {
        "id": 0,
        "state": "idle",        # idle | running | done | error
        "started_at": 0.0,
        "finished_at": 0.0,
        "done_html": "",
        "error": "",
    }
    app.config["cnfg_job_lock"] = threading.Lock()
    sock = Sock(app)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            profiles=profiles_mod.list_profiles(),
            schemas=schemas_mod.SCHEMAS,
            loader_labels=schemas_mod.LOADER_LABELS,
            ppcbug_niot_fields=ppcbug_mod.NIOT_USER_FIELDS,
            ppcbug_env_fields=ppcbug_mod.ENV_USER_FIELDS,
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
        resp = Response(
            stream_with_context(_sse(sess)),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "close",
            },
        )
        # Disable Flask's default HTTP/1.1 connection reuse for this
        # endpoint -- the SSE stream needs its own dedicated TCP
        # connection so the worker isn't asked to multiplex SSE chunks
        # with other XHRs from the same client.
        resp.headers["Connection"] = "close"
        return resp

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
        """VxWorks edit form. PPCBug is split into NIOT/ENV dialogs."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        profile = profiles_mod.get_profile(sess.profile.name) or sess.profile
        sess.profile = profile
        if profile.loader_hint == "ppcbug":
            # Defensive: shouldn't be reachable from the action bar
            # for PPCBug, but redirect to NIOT if it is.
            return params_edit_section("niot")
        fields = schemas_mod.fields_for(profile.loader_hint)
        return render_template(
            "_params_edit.html",
            profile=profile,
            fields=fields,
        )

    # ---- PPCBug NIOT / ENV split (one dialog per dialogue) -----------
    # Both sections share the same shape: open a dialog pre-filled with
    # the saved values; submit posts param_* form fields, the route
    # saves them to profile.boot_params, walks just that PPCBug
    # dialogue, reads back, renders a verify panel.

    def _ppcbug_section_fields(section: str):
        """Return the (label, key) tuple for a PPCBug section."""
        if section == "niot":
            return ppcbug_mod.NIOT_USER_FIELDS
        if section == "env":
            return ppcbug_mod.ENV_USER_FIELDS
        return ()

    def params_edit_section(section: str):
        """Open the NIOT or ENV edit dialog for the active session."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        profile = profiles_mod.get_profile(sess.profile.name) or sess.profile
        sess.profile = profile
        if profile.loader_hint != "ppcbug":
            return _error("NIOT/ENV only apply to PPCBug"), 400
        fields = _ppcbug_section_fields(section)
        if not fields:
            return _error(f"unknown section {section!r}"), 400
        return render_template(
            "_params_edit_section.html",
            profile=profile,
            fields=fields,
            section=section,
            section_label=section.upper(),
        )

    @app.get("/params/edit/niot")
    def params_edit_niot():
        return params_edit_section("niot")

    @app.get("/params/edit/env")
    def params_edit_env():
        return params_edit_section("env")

    def _ppcbug_section_save_to_profile(profile, section: str, form) -> tuple[dict, dict]:
        """Update profile.boot_params with this section's form fields.

        Returns (new_section_values, merged_boot_params). The OTHER
        section's saved values are preserved on disk. Empty form
        fields are treated as "delete this key from the saved set."
        """
        fields = _ppcbug_section_fields(section)
        section_keys = {key for _label, key in fields}
        new_section_values: dict[str, str] = {}
        for key in section_keys:
            v = (form.get(f"param_{key}") or "").strip()
            if v:
                new_section_values[key] = v
        merged = {
            k: v for k, v in profile.boot_params.items()
            if k not in section_keys
        }
        merged.update(new_section_values)
        profile.boot_params = merged
        profiles_mod.save_profile(profile)
        return new_section_values, merged

    def _ppcbug_section_push(section: str):
        """Save form values for one PPCBug section, push, verify."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _error(f"not at a loader prompt (state={ws.state})"), 409
        loader = ws.loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error("NIOT/ENV only apply to PPCBug"), 400
        fields = _ppcbug_section_fields(section)
        if not fields:
            return _error(f"unknown section {section!r}"), 400

        new_section_values, _merged = _ppcbug_section_save_to_profile(
            sess.profile, section, request.form
        )

        lock: threading.Lock = app.config["push_lock"]
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409

        # Pick the right driver pair for this section.
        if section == "niot":
            write_fn = ppcbug_mod.write_niot
            read_fn = ppcbug_mod.read_niot
        else:
            write_fn = ppcbug_mod.write_env
            read_fn = ppcbug_mod.read_env

        transport = sess.transport
        profile = sess.profile
        values_to_push = new_section_values

        job = app.config["push_job"]
        job_lock: threading.Lock = app.config["push_job_lock"]
        with job_lock:
            job["id"] += 1
            job["state"] = "running"
            job["started_at"] = time.time()
            job["finished_at"] = 0.0
            job["verify_html"] = ""
            job["error"] = ""

        def _runner():
            try:
                write_fn(transport, values_to_push)
                verify = read_fn(transport)
                diff = _diff_section(
                    fields, values_to_push, verify.params, loader="ppcbug"
                )
                with app.app_context():
                    html = render_template(
                        "_params_verify.html",
                        profile=profile,
                        fields=fields,
                        current=verify.params,
                        wrote=values_to_push,
                        diff=diff,
                    )
                with job_lock:
                    job["state"] = "done"
                    job["verify_html"] = html
                    job["finished_at"] = time.time()
            except Exception as e:
                with job_lock:
                    job["state"] = "error"
                    job["error"] = str(e)
                    job["finished_at"] = time.time()
            finally:
                lock.release()

        t = threading.Thread(target=_runner, daemon=True, name=f"push-{section}-{job['id']}")
        t.start()
        return render_template("_params_running.html", profile=profile)

    @app.post("/params/push/niot")
    def params_push_niot():
        return _ppcbug_section_push("niot")

    @app.post("/params/push/env")
    def params_push_env():
        return _ppcbug_section_push("env")

    def _ppcbug_section_save_only(section: str):
        """Save form values for one PPCBug section. No push.

        Used by the connected-mode 'Save' button (returns the params
        panel) and by the home-page section save forms (returns the
        profile list).
        """
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        if sess.profile.loader_hint != "ppcbug":
            return _error("NIOT/ENV only apply to PPCBug"), 400
        fields = _ppcbug_section_fields(section)
        if not fields:
            return _error(f"unknown section {section!r}"), 400
        _ppcbug_section_save_to_profile(sess.profile, section, request.form)
        # Re-render the same edit dialog so the user sees the saved
        # values reflected (and can continue editing or push).
        return render_template(
            "_params_edit_section.html",
            profile=sess.profile,
            fields=fields,
            section=section,
            section_label=section.upper(),
        )

    @app.post("/params/save/niot")
    def params_save_niot():
        return _ppcbug_section_save_only("niot")

    @app.post("/params/save/env")
    def params_save_env():
        return _ppcbug_section_save_only("env")

    @app.post("/profiles/<name>/save/<section>")
    def profile_section_save(name: str, section: str):
        """Home-page per-section save. No push (no session needed)."""
        profile = profiles_mod.get_profile(name)
        if profile is None:
            return _error(f"no such profile: {name!r}"), 404
        if profile.loader_hint != "ppcbug":
            return _error("NIOT/ENV only apply to PPCBug"), 400
        if section not in ("niot", "env"):
            return _error(f"unknown section {section!r}"), 400
        _ppcbug_section_save_to_profile(profile, section, request.form)
        # Reflect the updated profile in the home page.
        return _render_profile_list()

    @app.post("/params/push")
    def params_push():
        """Push the saved profile boot_params to the board (VxWorks only).

        PPCBug uses the split NIOT/ENV push routes instead, so its push
        button is on the action bar via /params/push/niot|env.
        """
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _error(f"not at a loader prompt (state={ws.state})"), 409
        loader = ws.loader or sess.profile.loader_hint
        if loader == "ppcbug":
            return _error("use the NIOT/ENV buttons for PPCBug pushes"), 400
        driver = _driver_for(loader)
        if driver is None:
            return _error(f"no driver for loader {loader!r}"), 400

        lock: threading.Lock = app.config["push_lock"]
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409

        values = dict(sess.profile.boot_params)
        fields = schemas_mod.fields_for(loader)
        transport = sess.transport
        profile = sess.profile

        job = app.config["push_job"]
        job_lock: threading.Lock = app.config["push_job_lock"]
        with job_lock:
            job["id"] += 1
            job["state"] = "running"
            job["started_at"] = time.time()
            job["finished_at"] = 0.0
            job["verify_html"] = ""
            job["error"] = ""

        def _runner():
            try:
                driver.write_params(transport, values)
                verify = driver.read_params(transport)
                diff = _diff_section(fields, values, verify.params, loader=loader)
                with app.app_context():
                    html = render_template(
                        "_params_verify.html",
                        profile=profile,
                        fields=fields,
                        current=verify.params,
                        wrote=values,
                        diff=diff,
                    )
                with job_lock:
                    job["state"] = "done"
                    job["verify_html"] = html
                    job["finished_at"] = time.time()
            except Exception as e:
                with job_lock:
                    job["state"] = "error"
                    job["error"] = str(e)
                    job["finished_at"] = time.time()
            finally:
                lock.release()

        t = threading.Thread(target=_runner, daemon=True, name=f"push-{job['id']}")
        t.start()
        return render_template("_params_running.html", profile=profile)

    @app.get("/params/push/status")
    def params_push_status():
        """Return the latest push job state. While running, returns a
        "still running" snippet for hx-swap; when done, returns the
        verify panel; on error, returns an error snippet."""
        job = app.config["push_job"]
        job_lock: threading.Lock = app.config["push_job_lock"]
        with job_lock:
            state = job["state"]
            html = job["verify_html"]
            err = job["error"]
        if state == "done":
            return html
        if state == "error":
            sess = app.config["sessions"].current()
            return f'<div class="error">push failed: {err}</div>' + render_template(
                "_params_actions.html",
                profile=(sess.profile if sess else None),
            )
        # running or idle: keep showing the running panel; client polls.
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        return render_template(
            "_params_running.html",
            profile=(sess.profile if sess else None),
        )

    @app.get("/params/diag")
    def params_diag_form():
        """Show the diag-command picker dialog."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        loader = sess.watcher.status().loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error(f"diag only supported on PPCBug (loader={loader!r})"), 400
        return render_template(
            "_params_diag_form.html",
            profile=sess.profile,
            diag_commands=ppcbug_mod.DIAG_COMMANDS,
        )

    @app.post("/params/diag")
    def params_diag():
        """Run the diag commands selected in the form.

        Reads diag_<key>=1 form fields (no profile persistence).
        Sends SD -> each selected command -> SD. Output is live in the
        terminal; this route returns a small status panel.
        """
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        loader = sess.watcher.status().loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error(f"diag only supported on PPCBug (loader={loader!r})"), 400
        valid = {key for _label, key, _cmd in ppcbug_mod.DIAG_COMMANDS}
        enabled = {
            k[len("diag_"):] for k in request.form.keys()
            if k.startswith("diag_") and k[len("diag_"):] in valid
        }
        if not enabled:
            return _error("no diag commands selected; tick at least one box"), 400

        lock: threading.Lock = app.config["push_lock"]
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409
        transport = sess.transport
        profile = sess.profile

        def _runner():
            try:
                ppcbug_mod.diag(transport, enabled)
            except Exception as e:
                print(f"[diag] error: {e}", file=__import__("sys").stderr, flush=True)
            finally:
                lock.release()

        t = threading.Thread(target=_runner, daemon=True, name="diag")
        t.start()
        return render_template("_params_diag.html", profile=profile, enabled=sorted(enabled))

    @app.get("/params/cnfg")
    def params_cnfg_form():
        """Show the one-shot VPD repair form (CNFG;M).

        Reads the board's current VPD via `CNFG` first so the form can
        pre-fill each field with whatever is on the board today --
        garbage values mean a corrupt block that needs repair; sane
        values let the user spot which fields actually need editing.
        """
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        loader = ws.loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error(f"CNFG only supported on PPCBug (loader={loader!r})"), 400

        current: dict[str, str] = {}
        read_error = ""
        lock: threading.Lock = app.config["push_lock"]
        if lock.acquire(blocking=False):
            try:
                result = ppcbug_mod.read_cnfg(sess.transport)
                current = result.params
                if not current:
                    read_error = (
                        f"CNFG returned no parsable fields "
                        f"(buffer was {len(result.raw)} bytes; "
                        f"check the terminal -- the board may not be at PPC1-Bug>)"
                    )
            except Exception as e:
                read_error = str(e)
            finally:
                lock.release()
        else:
            read_error = "another operation is running; current values not read"

        return render_template(
            "_params_cnfg.html",
            profile=sess.profile,
            current=current,
            read_error=read_error,
        )

    @app.post("/params/cnfg/run")
    def params_cnfg_run():
        """Kick off `CNFG;M` in a background thread. Values come from the
        form and are NOT saved to the profile -- VPD is per-board."""
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _error(f"not at a loader prompt (state={ws.state})"), 409
        loader = ws.loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error(f"CNFG only supported on PPCBug (loader={loader!r})"), 400

        # Collect vpd_<key> form fields. Empty values mean "keep current".
        values: dict[str, str] = {}
        for _label, key in ppcbug_mod.CNFG_FIELDS:
            v = (request.form.get(f"vpd_{key}") or "").strip()
            if v:
                values[key] = v
        if not values:
            return _error("nothing to write; fill in at least one field"), 400

        lock: threading.Lock = app.config["push_lock"]
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409
        transport = sess.transport
        profile = sess.profile

        job = app.config["cnfg_job"]
        job_lock: threading.Lock = app.config["cnfg_job_lock"]
        with job_lock:
            job["id"] += 1
            job["state"] = "running"
            job["started_at"] = time.time()
            job["finished_at"] = 0.0
            job["done_html"] = ""
            job["error"] = ""

        def _runner():
            try:
                result = ppcbug_mod.write_cnfg(transport, values)
                with app.app_context():
                    html = render_template(
                        "_params_cnfg_done.html",
                        profile=profile,
                        fields_written=result.fields_written,
                    )
                with job_lock:
                    job["state"] = "done"
                    job["done_html"] = html
                    job["finished_at"] = time.time()
            except Exception as e:
                with job_lock:
                    job["state"] = "error"
                    job["error"] = str(e)
                    job["finished_at"] = time.time()
            finally:
                lock.release()

        t = threading.Thread(target=_runner, daemon=True, name=f"cnfg-{job['id']}")
        t.start()
        return render_template("_params_cnfg_running.html", profile=profile)

    @app.get("/params/cnfg/status")
    def params_cnfg_status():
        job = app.config["cnfg_job"]
        job_lock: threading.Lock = app.config["cnfg_job_lock"]
        with job_lock:
            state = job["state"]
            html = job["done_html"]
            err = job["error"]
        if state == "done":
            return html
        if state == "error":
            sess = app.config["sessions"].current()
            return f'<div class="error">CNFG;M failed: {err}</div>' + render_template(
                "_params_actions.html",
                profile=(sess.profile if sess else None),
            )
        sessions: SessionManager = app.config["sessions"]
        sess = sessions.current()
        return render_template(
            "_params_cnfg_running.html",
            profile=(sess.profile if sess else None),
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


def _diff_section(fields, wrote: dict[str, str], got: dict[str, str], loader: str) -> list[dict]:
    """Compute the verify-panel diff for one PPCBug dialogue section.

    Mirrors the original /params/push logic: skip keep-current, treat
    `.` as clear (with the PPCBug NULL-only-on-file-name caveat), and
    treat single-char Y/N values as case-insensitive on PPCBug.
    """
    out: list[dict] = []
    for _label, key in fields:
        want = wrote.get(key, "")
        seen = got.get(key, "")
        if not want:
            continue
        if want == ".":
            if loader == "ppcbug" and key not in (
                "boot_file_name",
                "argument_file_name",
            ):
                continue
            if seen and seen.upper() != "NULL":
                out.append({"key": key, "want": "(cleared)", "got": seen})
            continue
        if len(want) == 1 and want.casefold() == seen.casefold():
            continue
        if want != seen:
            out.append({"key": key, "want": want, "got": seen})
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
        f"[sse] subscriber attached for {sess.profile.name} "
        f"({transport.host}:{transport.port})",
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
            now = time.time()
            if q:
                # Coalesce every currently-queued chunk into ONE yield
                # to minimize the number of writes the gevent worker
                # has to make. Per-chunk yields here block when the
                # client's TCP receive window is small / contended,
                # causing the SSE generator to deadlock on a yield.
                merged = bytearray()
                drained = 0
                while q:
                    merged.extend(q.popleft())
                    drained += 1
                chunks_sent += drained
                yield f"event: chunk\ndata: {_b64(bytes(merged))}\n\n"
            else:
                if now - last_keepalive > 1.0:
                    yield ": keepalive\n\n"
                    last_keepalive = now
                time.sleep(0.05)
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
