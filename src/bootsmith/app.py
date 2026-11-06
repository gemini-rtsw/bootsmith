from __future__ import annotations

import os
import re
import secrets
import sys
import threading
import time
from dataclasses import asdict

from flask import (
    Flask, Response, current_app, g, jsonify, render_template, request, stream_with_context,
)
from flask_sock import Sock
from markupsafe import escape

from . import ppcbug as ppcbug_mod
from . import profiles as profiles_mod
from . import schemas as schemas_mod
from . import vxworks as vxworks_mod
from .session import ConnectCancelled, PortBusyError, PortInUseError, SessionManager

# Browser identity for multi-session support: each browser gets a random
# id cookie and its own session (WTI connection, push/CNFG jobs).
SID_COOKIE = "bootsmith_sid"
_SID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
TAB_HEADER = "X-Bootsmith-Tab"
_TAB_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")


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


def _render_profile_list(
    saved_profile: str = "", saved_section: str = "", open_system: str = ""
):
    """Render the home-page profile list.

    Pass saved_profile/saved_section so the just-edited form can
    show a 'Saved' badge next to its Save button. The edit form for
    that profile stays open (un-hidden) so the user sees the badge
    without having to click Edit again.
    """
    return render_template(
        "_profile_list.html",
        **_list_context(),
        saved_profile=saved_profile,
        saved_section=saved_section,
        saved_at=time.strftime("%H:%M:%S") if saved_profile else "",
        open_system=open_system,
    )


def _list_context() -> dict:
    """Template context for the target list (system -> connection -> preset)."""
    profiles = profiles_mod.list_profiles()
    return dict(
        profiles=profiles,
        system_of=profiles_mod.system_of,
        systems=profiles_mod.group_by_system(profiles),
        port_viewers=current_app.config["sessions"].port_viewers(),
        schemas=schemas_mod.SCHEMAS,
        loader_labels=schemas_mod.LOADER_LABELS,
        ppcbug_niot_fields=ppcbug_mod.NIOT_USER_FIELDS,
        ppcbug_env_fields=ppcbug_mod.ENV_USER_FIELDS,
    )


def create_app() -> Flask:
    app = Flask(__name__)
    app.jinja_env.globals["param_help"] = schemas_mod.help_for
    app.jinja_env.globals["reboot_spec"] = profiles_mod.reboot_spec
    # The unit passes the pinned image (e.g. ghcr.io/...:0.1.0-git<hash>) so
    # the page can show which build is running.
    app.jinja_env.globals["running_image"] = os.environ.get("BOOTSMITH_IMAGE", "")
    # One session per browser tab, keyed by _sid(). Push/CNFG
    # job state and the push lock live on each Session (see session.py).
    app.config["sessions"] = SessionManager()
    sock = Sock(app)

    def _sid() -> str:
        """Session key: the browser cookie plus the page's tab id.

        Tabs of one browser share the cookie, so the page also sends a
        per-tab id (TAB_HEADER, or ?tab= where a header can't be set, as
        with EventSource). Without it two tabs would be one session and
        the second Connect would replace the first tab's connection.
        """
        sid = g.get("bootsmith_sid")
        if sid:
            return sid
        cookie = request.cookies.get(SID_COOKIE, "")
        if not _SID_RE.fullmatch(cookie):
            cookie = secrets.token_urlsafe(24)
            g.bootsmith_new_sid = True
        g.bootsmith_cookie = cookie
        tab = request.headers.get(TAB_HEADER) or request.args.get("tab", "")
        sid = f"{cookie}.{tab}" if _TAB_RE.fullmatch(tab) else cookie
        g.bootsmith_sid = sid
        return sid

    def _current_session():
        return app.config["sessions"].current(_sid())

    @app.before_request
    def _ensure_sid():
        _sid()

    @app.after_request
    def _set_sid_cookie(resp):
        if g.get("bootsmith_new_sid"):
            resp.set_cookie(SID_COOKIE, g.bootsmith_cookie, httponly=True, samesite="Lax")
        return resp

    # Free the WTI ports of browsers that went away (closed tab, crash).
    # Every 5 s so a closed tab (see /session/leave) frees the port within
    # ~15 s; idle sessions still need IDLE_TIMEOUT_S.
    def _reaper():
        while True:
            time.sleep(5)
            try:
                app.config["sessions"].reap_idle()
            except Exception:
                pass

    threading.Thread(target=_reaper, name="session-reaper", daemon=True).start()

    @app.get("/help")
    def help_page():
        """How to use bootsmith, save presets to git and roll out a version."""
        return render_template("help.html")

    @app.get("/")
    def index():
        return render_template("index.html", **_list_context())

    @app.get("/profiles")
    def list_profiles_route():
        return _render_profile_list()

    def _system_or_404(system: str):
        system = (system or "").strip().upper()
        if not profiles_mod.system_exists(system):
            return None, (_error(f"no such system: {system!r}"), 404)
        return system, None

    def _system_connections(system: str) -> dict[str, profiles_mod.TerminalServer]:
        """The system's connections keyed "host:port", named as the list shows them."""
        groups = [g for g in profiles_mod.group_by_system(profiles_mod.list_profiles())
                  if g.name == system]
        out: dict[str, profiles_mod.TerminalServer] = {}
        for c in (groups[0].connections if groups else []):
            out[f"{c.host}:{c.port}"] = profiles_mod.TerminalServer(c.name, c.host, c.port)
        return out

    def _used_on(form, system: str) -> list[profiles_mod.TerminalServer]:
        """The ticked "Used on" connections of a preset form, in list order."""
        conns = _system_connections(system)
        picked = set(form.getlist("used_on"))
        unknown = picked - set(conns)
        if unknown:
            raise ValueError(f"not connections of {system}: {', '.join(sorted(unknown))}")
        out = [ts for key, ts in conns.items() if key in picked]
        if not out:
            raise ValueError("tick at least one connection this preset is used on")
        return out

    def _new_preset(name: str, system: str, loader_hint: str, terminal_servers, form):
        if not name:
            return _error("name is required"), 400
        if not profiles_mod._NAME_RE.match(name):
            return _error("preset name: letters, digits, '.', '_' or '-', max 64"), 400
        if loader_hint not in VALID_LOADERS:
            return _error(
                f"loader is required; pick one of {sorted(VALID_LOADERS)}"
            ), 400
        if profiles_mod.get_profile(name) is not None:
            return _error(f"preset {name!r} already exists"), 409
        profile = profiles_mod.Profile(
            name=name,
            system=system,
            terminal_servers=terminal_servers,
            loader_hint=loader_hint,
            boot_params=_collect_boot_params(form, loader_hint),
        )
        err = _set_reboot(profile, form)
        if err:
            return err
        profiles_mod.save_profile(profile)
        return _render_profile_list(open_system=system)

    @app.post("/systems")
    def create_system():
        """New system: its first preset and first connection in one go."""
        try:
            system = profiles_mod.normalize_system(request.form.get("system", ""))
        except ValueError as e:
            return _error(str(e)), 400
        if profiles_mod.system_exists(system):
            return _error(f"system {system} already exists; add presets inside it"), 409
        try:
            terminal_servers = _collect_terminal_servers(request.form)
        except ValueError as e:
            return _error(str(e)), 400
        if len(terminal_servers) != 1:
            return _error("give the system's first connection: name, host and port"), 400
        return _new_preset(
            (request.form.get("name") or "").strip(), system,
            (request.form.get("loader_hint") or "").strip(), terminal_servers, request.form,
        )

    @app.post("/systems/<system>/presets")
    def create_system_preset(system: str):
        system, err = _system_or_404(system)
        if err:
            return err
        try:
            terminal_servers = _used_on(request.form, system)
        except ValueError as e:
            return _error(str(e)), 400
        return _new_preset(
            (request.form.get("name") or "").strip(), system,
            (request.form.get("loader_hint") or "").strip(), terminal_servers, request.form,
        )

    def _connection_form() -> profiles_mod.TerminalServer:
        name = (request.form.get("conn_name") or "").strip()
        host = (request.form.get("conn_host") or "").strip()
        port = (request.form.get("conn_port") or "").strip()
        if not name or not host or not port:
            raise ValueError("a connection needs a name, host and port")
        try:
            port_int = int(port)
        except ValueError:
            raise ValueError(f"port must be an integer: {port!r}")
        if not 1 <= port_int <= 65535:
            raise ValueError(f"port out of range: {port_int}")
        return profiles_mod.TerminalServer(name, host, port_int)

    def _old_key() -> tuple[str, int]:
        try:
            return (request.form["old_host"], int(request.form["old_port"]))
        except (KeyError, ValueError):
            raise ValueError("which connection? (old_host/old_port missing)")

    @app.post("/systems/<system>/connections")
    def create_connection(system: str):
        system, err = _system_or_404(system)
        if err:
            return err
        try:
            profiles_mod.set_connection(
                system, None, _connection_form(), request.form.getlist("used_by")
            )
        except ValueError as e:
            return _error(str(e)), 400
        return _render_profile_list(open_system=system)

    @app.post("/systems/<system>/connections/update")
    def update_connection(system: str):
        system, err = _system_or_404(system)
        if err:
            return err
        try:
            old = _old_key()
            profiles_mod.set_connection(
                system, old, _connection_form(), request.form.getlist("used_by")
            )
        except ValueError as e:
            return _error(str(e)), 400
        return _render_profile_list(open_system=system)

    @app.post("/systems/<system>/connections/delete")
    def delete_connection(system: str):
        system, err = _system_or_404(system)
        if err:
            return err
        try:
            profiles_mod.delete_connection(system, _old_key())
        except ValueError as e:
            return _error(str(e)), 400
        return _render_profile_list(open_system=system)

    @app.post("/profiles/<name>/duplicate")
    def duplicate_profile_route(name: str):
        try:
            copy = profiles_mod.duplicate_profile(name)
        except FileNotFoundError:
            return _error(f"no such profile: {name!r}"), 404
        # Re-render with the new copy's edit form already open so the user
        # can immediately rename it and tweak the fields that differ.
        return _render_profile_list(saved_profile=copy.name, saved_section="")

    @app.post("/profiles/<name>/delete")
    def delete_profile_route(name: str):
        profiles_mod.delete_profile(name)
        return _render_profile_list()

    @app.post("/profiles/<name>/update")
    def update_profile_route(name: str):
        existing = profiles_mod.get_profile(name)
        if existing is None:
            return _error(f"no such profile: {name!r}"), 404
        loader_hint = (request.form.get("loader_hint") or existing.loader_hint).strip()
        if loader_hint not in VALID_LOADERS:
            return _error(
                f"loader is required; pick one of {sorted(VALID_LOADERS)}"
            ), 400
        # ts_name is only present when the Connection form (which owns the
        # terminal-server rows) was submitted; the NIOT/ENV/boot-params
        # forms post to this same endpoint without it, and must leave the
        # existing terminal servers untouched.
        # The preset dialog sends its "Used on" checkboxes (and the marker
        # used_on_sent, since an all-unticked group sends nothing).
        system = profiles_mod.system_of(existing)
        existing.system = system
        if "used_on_sent" in request.form:
            try:
                existing.terminal_servers = _used_on(request.form, system)
            except ValueError as e:
                return _error(str(e)), 400
        elif "ts_name" in request.form:
            try:
                terminal_servers = _collect_terminal_servers(request.form)
            except ValueError as e:
                return _error(str(e)), 400
            if not terminal_servers:
                return _error("at least one terminal server is required"), 400
            existing.terminal_servers = terminal_servers
        loader_changed = loader_hint != existing.loader_hint
        existing.loader_hint = loader_hint
        err = _set_reboot(existing, request.form)
        if err:
            return err
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
            sess = _current_session()
            if sess is not None:
                sess.profile = existing
            fields = schemas_mod.fields_for(existing.loader_hint)
            return render_template(
                "_params_push.html",
                profile=existing,
                fields=fields,
            )
        # Pick a section name for the Saved badge:
        #   - "connection" if only connection fields were submitted
        #     (no param_* keys in the form);
        #   - "boot params" for VxWorks where everything is one section.
        saved_section = "connection" if not has_param_fields else "boot params"
        return _render_profile_list(
            saved_profile=existing.name, saved_section=saved_section,
        )

    @app.post("/session/open")
    def session_open():
        import sys as _sys

        name = (request.form.get("name") or "").strip()
        profile = profiles_mod.get_profile(name)
        if profile is None:
            return _error(f"no such profile: {name!r}"), 404
        if not profile.terminal_servers:
            return _error(f"profile {name!r} has no terminal servers configured"), 400
        ts_name = (request.form.get("terminal_server") or "").strip()
        if ts_name:
            terminal_server = profile.terminal_server(ts_name)
            if terminal_server is None:
                return _error(f"no such terminal server: {ts_name!r}"), 400
        elif len(profile.terminal_servers) == 1:
            terminal_server = profile.terminal_servers[0]
        else:
            return _error("multiple terminal servers configured; choose one"), 400
        sessions: SessionManager = app.config["sessions"]
        try:
            sessions.open(_sid(), profile, terminal_server)
        except PortInUseError as e:
            return _error(str(e)), 409
        except ConnectCancelled as e:
            return _error(str(e)), 409
        except Exception as e:
            print(
                f"[session/open] failed for {name!r} -> "
                f"{terminal_server.host}:{terminal_server.port}: {type(e).__name__}: {e}",
                file=_sys.stderr,
                flush=True,
            )
            return _error(str(e)), 400
        return _render_session(sessions.current(_sid()))

    def _render_session(sess):
        """The connected view, with the presets this tab can switch to:
        every preset of the same system that uses this port."""
        key = (sess.terminal_server.host, sess.terminal_server.port)
        system = profiles_mod.system_of(sess.profile)
        presets = [
            p for p in profiles_mod.list_profiles()
            if profiles_mod.system_of(p) == system
            and any((t.host, t.port) == key for t in p.terminal_servers)
        ]
        return render_template(
            "_session.html",
            profile=sess.profile,
            terminal_server=sess.terminal_server,
            system=system,
            presets=presets,
        )

    @app.post("/session/preset")
    def session_preset():
        name = (request.form.get("name") or "").strip()
        profile = profiles_mod.get_profile(name)
        if profile is None:
            return _error(f"no such profile: {name!r}"), 404
        cur = app.config["sessions"].current(_sid())
        if cur is not None and profiles_mod.system_of(profile) != profiles_mod.system_of(cur.profile):
            return _error(
                f"{profile.name} is a {profiles_mod.system_of(profile)} preset, "
                f"not {profiles_mod.system_of(cur.profile)}"
            ), 400
        try:
            sess = app.config["sessions"].set_profile(_sid(), profile)
        except LookupError as e:
            return _error(str(e)), 404
        except ValueError as e:
            return _error(str(e)), 400
        return _render_session(sess)

    @app.post("/session/cancel")
    def session_cancel():
        sessions: SessionManager = app.config["sessions"]
        sessions.cancel_connect(_sid())
        return "", 204

    @app.post("/session/leave")
    def session_leave():
        """Beacon from the page's pagehide: this tab is going away.

        Sent with navigator.sendBeacon, which can't set headers, so the
        tab id comes as ?tab= (see _sid). Not _current_session(): that
        would count this request as activity and cancel the leave.
        """
        app.config["sessions"].leave(_sid())
        return ("", 204)

    @app.post("/session/close")
    def session_close():
        sessions: SessionManager = app.config["sessions"]
        sessions.close(_sid())
        return _render_profile_list()

    @app.post("/connections/disconnect-all")
    def connections_disconnect_all():
        """Free a WTI port held by bootsmith: drop every tab on it."""
        try:
            key = (request.form["host"].strip(), int(request.form["port"]))
        except (KeyError, ValueError):
            return _error("host and port are required"), 400
        try:
            n = app.config["sessions"].disconnect_port(key, by=request.remote_addr or "")
        except PortBusyError as e:
            return _error(str(e)), 409
        print(
            f"[connections/disconnect-all] {key[0]}:{key[1]}: {n} tab(s) dropped "
            f"by {request.remote_addr}",
            file=sys.stderr, flush=True,
        )
        return _render_profile_list()

    @app.get("/session/status")
    def session_status():
        sess = _current_session()
        if sess is None:
            return jsonify({"open": False, "notice": app.config["sessions"].take_notice(_sid())})
        ws = sess.watcher.status()
        return jsonify(
            {
                "open": True,
                "profile": sess.profile.name,
                "state": ws.state,
                "loader": ws.loader,
                "abort_chars_sent": ws.abort_chars_sent,
                "notes": ws.notes,
                "viewers": app.config["sessions"].viewers(sess),
                "transport": asdict(sess.transport.status()),
            }
        )

    @app.post("/session/rearm")
    def session_rearm():
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        sess.watcher.rearm()
        return ("", 204)

    @app.post("/session/force-prompt")
    def session_force_prompt():
        sess = _current_session()
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
        sess = _current_session()
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
        sess = _current_session()
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

        sess = _current_session()
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
        sess = _current_session()
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
        sess = _current_session()
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
        sess = _current_session()
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

    def _lock_at_prompt(sess):
        """Take the push lock and check the board answers with its boot prompt.

        Returns None with the lock HELD (caller releases it), or an error
        response with the lock released. The watcher state alone isn't
        enough: after a Boot or a reboot from the shell it can still say
        at_prompt, and a push then types boot params into the running OS
        (seen 2026-11-05 in the team demo).
        """
        lock: threading.Lock = sess.push_lock
        if not lock.acquire(blocking=False):
            return _error("another push is already in progress"), 409
        try:
            loader = sess.watcher.probe_prompt()
        except ConnectionError as e:
            lock.release()
            return _error(f"WTI session is dead: {e}. Click Reconnect."), 502
        if loader is None:
            lock.release()
            return _not_at_prompt(sess)
        return None

    def _not_at_prompt(sess):
        return _error(
            f"{sess.profile.name} is not at its boot prompt (it is probably "
            "running its OS), so nothing was sent. Reboot it to get back to "
            "the boot prompt."
        ), 409

    def _ppcbug_section_push(section: str):
        """Save form values for one PPCBug section, push, verify."""
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _not_at_prompt(sess)
        loader = ws.loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error("NIOT/ENV only apply to PPCBug"), 400
        fields = _ppcbug_section_fields(section)
        if not fields:
            return _error(f"unknown section {section!r}"), 400

        new_section_values, _merged = _ppcbug_section_save_to_profile(
            sess.profile, section, request.form
        )

        refused = _lock_at_prompt(sess)
        if refused:
            return refused
        lock: threading.Lock = sess.push_lock

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

        job = sess.push_job
        job_lock: threading.Lock = sess.push_job_lock
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
        sess = _current_session()
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
            saved_at=time.strftime("%H:%M:%S"),
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
        # Reflect the updated profile in the home page, with a Saved
        # badge next to the section's Save button.
        return _render_profile_list(saved_profile=profile.name, saved_section=section)

    @app.post("/params/push")
    def params_push():
        """Push the saved profile boot_params to the board (VxWorks only).

        PPCBug uses the split NIOT/ENV push routes instead, so its push
        button is on the action bar via /params/push/niot|env.
        """
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _not_at_prompt(sess)
        loader = ws.loader or sess.profile.loader_hint
        if loader == "ppcbug":
            return _error("use the NIOT/ENV buttons for PPCBug pushes"), 400
        driver = _driver_for(loader)
        if driver is None:
            return _error(f"no driver for loader {loader!r}"), 400

        refused = _lock_at_prompt(sess)
        if refused:
            return refused
        lock: threading.Lock = sess.push_lock

        values = dict(sess.profile.boot_params)
        fields = schemas_mod.fields_for(loader)
        transport = sess.transport
        profile = sess.profile

        job = sess.push_job
        job_lock: threading.Lock = sess.push_job_lock
        with job_lock:
            job["id"] += 1
            job["state"] = "running"
            job["started_at"] = time.time()
            job["finished_at"] = 0.0
            job["verify_html"] = ""
            job["error"] = ""

        def _runner():
            try:
                result = driver.write_params(transport, values)
                verify = driver.read_params(transport)
                # Only check fields the dialogue actually prompted for. A
                # profile can carry fields this board's boot ROM doesn't have
                # (e.g. unit_number on mv167), which would otherwise always
                # read back blank and be flagged as a failed write.
                written = {k: values[k] for k in result.fields_written if k in values}
                diff = _diff_section(fields, written, verify.params, loader=loader)
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
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        job = sess.push_job
        job_lock: threading.Lock = sess.push_job_lock
        with job_lock:
            state = job["state"]
            html = job["verify_html"]
            err = job["error"]
        if state == "done":
            return html
        if state == "error":
            return f'<div class="error">push failed: {escape(err)}</div>' + render_template(
                "_params_actions.html",
                profile=sess.profile,
            )
        # running or idle: keep showing the running panel; client polls.
        return render_template(
            "_params_running.html",
            profile=sess.profile,
        )

    @app.get("/params/diag")
    def params_diag_form():
        """Show the diag-command picker dialog."""
        sess = _current_session()
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
        sess = _current_session()
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

        refused = _lock_at_prompt(sess)
        if refused:
            return refused
        lock: threading.Lock = sess.push_lock
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
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        loader = ws.loader or sess.profile.loader_hint
        if loader != "ppcbug":
            return _error(f"CNFG only supported on PPCBug (loader={loader!r})"), 400

        current: dict[str, str] = {}
        read_error = ""
        lock: threading.Lock = sess.push_lock
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
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        ws = sess.watcher.status()
        if ws.state != "at_prompt":
            return _not_at_prompt(sess)
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

        refused = _lock_at_prompt(sess)
        if refused:
            return refused
        lock: threading.Lock = sess.push_lock
        transport = sess.transport
        profile = sess.profile

        job = sess.cnfg_job
        job_lock: threading.Lock = sess.cnfg_job_lock
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
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        job = sess.cnfg_job
        job_lock: threading.Lock = sess.cnfg_job_lock
        with job_lock:
            state = job["state"]
            html = job["done_html"]
            err = job["error"]
        if state == "done":
            return html
        if state == "error":
            return f'<div class="error">CNFG;M failed: {escape(err)}</div>' + render_template(
                "_params_actions.html",
                profile=sess.profile,
            )
        return render_template(
            "_params_cnfg_running.html",
            profile=sess.profile,
        )

    @app.post("/params/boot")
    def params_boot():
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        loader = sess.watcher.status().loader or sess.profile.loader_hint
        driver = _driver_for(loader)
        if driver is not None:
            refused = _lock_at_prompt(sess)
            if refused:
                return refused
            try:
                driver.boot(sess.transport)
            except ConnectionError as e:
                return _error(f"WTI session is dead during boot: {e}."), 502
            finally:
                sess.push_lock.release()
            # The board is leaving its boot prompt. Without this the watcher
            # kept saying at_prompt and the next push went into the OS shell.
            sess.watcher.mark_running()
        # Stay connected so the user can watch the boot scroll past in the
        # live serial pane. Render a small status panel that replaces the
        # params section but keeps the session open.
        return render_template(
            "_params_booting.html",
            profile=sess.profile,
            fields=schemas_mod.fields_for(sess.profile.loader_hint),
        )

    @app.post("/session/reboot")
    def session_reboot():
        """Reboot the crate from its running OS with the preset's reboot keys.

        Only from the OS: a probe CR first, and if the boot prompt answers
        nothing is sent (Boot is the button for that; `exit` typed into
        PPCBug or the vxWorks boot ROM means something else). Afterwards the
        watcher waits for the reboot banner and stops the auto-boot as usual.
        """
        sess = _current_session()
        if sess is None:
            return _error("no session open"), 404
        spec = profiles_mod.reboot_spec(sess.profile)
        if not spec:
            return _error(
                f"{sess.profile.name} has no reboot set, so nothing was sent. Edit "
                "the preset and fill in Reboot (e.g. exit, or dbpf <ioc>:SysReset 1). "
                "A crate that only reboots from its bootbar can't be rebooted here."
            ), 409
        try:
            data = profiles_mod.reboot_bytes(spec)
        except ValueError as e:
            return _error(f"preset reboot is invalid: {e}"), 400
        lock: threading.Lock = sess.push_lock
        if not lock.acquire(blocking=False):
            return _error("a push is in progress; reboot after it finishes"), 409
        try:
            if sess.watcher.probe_prompt() is not None:
                return _error(
                    f"{sess.profile.name} is at its boot prompt, so the reboot "
                    "was not sent. Use Boot to start it."
                ), 409
            sess.transport.write(data)
        except ConnectionError as e:
            return _error(f"WTI session is dead: {e}. Click Reconnect."), 502
        finally:
            lock.release()
        sess.watcher.rearm()
        print(
            f"[session/reboot] {sess.profile.name} via "
            f"{sess.terminal_server.host}:{sess.terminal_server.port}: sent {spec!r}",
            file=sys.stderr, flush=True,
        )
        return render_template("_params_rebooting.html", profile=sess.profile, spec=spec)

    @app.post("/session/send")
    def session_send():
        sess = _current_session()
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

        sess = _current_session()
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


def _set_reboot(profile, form):
    """Take the preset dialog's Reboot field, if it was sent. Returns an
    error response for a value that can't be typed, else None."""
    if "reboot" not in form:
        return None
    spec = form.get("reboot", "").strip()
    if spec:
        try:
            profiles_mod.reboot_bytes(spec)
        except ValueError as e:
            return _error(f"Reboot: {e}"), 400
    profile.reboot = spec
    return None


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


def _collect_terminal_servers(form) -> list[profiles_mod.TerminalServer]:
    """Pull the repeated ts_name/ts_host/ts_port rows into TerminalServers.

    Blank template rows (all three fields empty) are skipped so the
    "+ Add terminal server" row doesn't need to be filled in to submit.
    Raises ValueError with a user-facing message on any other problem.
    """
    names = form.getlist("ts_name")
    hosts = form.getlist("ts_host")
    ports = form.getlist("ts_port")
    servers: list[profiles_mod.TerminalServer] = []
    seen: set[str] = set()
    for raw_name, raw_host, raw_port in zip(names, hosts, ports):
        name = raw_name.strip()
        host = raw_host.strip()
        port = raw_port.strip()
        if not name and not host and not port:
            continue
        if not name or not host or not port:
            raise ValueError("each terminal server needs a name, host, and port")
        if name in seen:
            raise ValueError(f"duplicate terminal server name: {name!r}")
        seen.add(name)
        try:
            port_int = int(port)
        except ValueError:
            raise ValueError(f"terminal server port must be an integer: {port!r}")
        servers.append(profiles_mod.TerminalServer(name=name, host=host, port=port_int))
    return servers


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
    # The page shows this HTML in its error banner, and msg can carry
    # profile names, hostnames and exception text, so escape it.
    return Response(
        f'<div class="error">{escape(msg)}</div>',
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
