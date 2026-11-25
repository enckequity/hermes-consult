"""Config-driven consultation service: ``hermes consult serve``.

Everything it does follows configuration a portal (or a person) can write; there is no
Desktop or dashboard dependency at run time.

* Plugin settings, ``plugins.entries.hermes-consult.settings`` in config.yaml:
  ``enabled`` (default false), ``public_origin``, ``port`` (4318), ``proxy_hops`` (0),
  ``voice_enabled`` (default false), ``lead_handoff`` (``kanban`` | ``none``, default
  ``kanban``) and ``kanban_board`` (default: the current board).
* Sites, ``<HERMES_HOME>/consult/sites.json``: ``{"sites": [{"key": "main", "name": ...,
  "origins": [...], ...}]}``. The consultation server validates each site strictly; caps
  have defaults and cannot be turned off. A site removed from the file is paused.
* Status, ``<HERMES_HOME>/consult/status.json``: running state, errors and, per site, its
  public id and embed snippet.

The service re-reads both on every tick (default 5 s) and applies changes. While
``enabled`` is false it only writes status, so it can stay supervised all the time.
The consultation server listens on 127.0.0.1 only.
"""

import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from . import leads
from .runner import Connector

ROOT = Path(__file__).resolve().parents[1]
SITE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_SITES = 20
DEFAULTS = {
    "enabled": False,
    "public_origin": "",
    "port": 4318,
    "proxy_hops": 0,
    "voice_enabled": False,
    "lead_handoff": "kanban",
    "kanban_board": "",
}
SETTINGS = tuple(DEFAULTS)
TURNSTILE_KEY = "HERMES_CONSULT_TURNSTILE_SECRET"


def check_origin(value):
    """An HTTPS origin, or plain HTTP on loopback for local testing; ``None`` if invalid."""
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    loopback = parsed.hostname in ("127.0.0.1", "localhost")
    if parsed.scheme not in ("https", "http") or (parsed.scheme == "http" and not loopback):
        return None
    if not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.params:
        return None
    netloc = parsed.hostname.lower() + (":%d" % port if port else "")
    return parsed.scheme + "://" + netloc


def parse_settings(raw):
    """``(settings, errors)`` from the raw plugin settings mapping."""
    raw = raw if isinstance(raw, dict) else {}
    settings, errors = dict(DEFAULTS), []

    def take(key, valid, message):
        value = raw.get(key)
        if value is None:
            return
        if valid(value):
            settings[key] = value
        else:
            errors.append("%s %s" % (key, message))

    is_int = lambda v: isinstance(v, int) and not isinstance(v, bool)
    take("enabled", lambda v: isinstance(v, bool), "must be true or false")
    take("voice_enabled", lambda v: isinstance(v, bool), "must be true or false")
    take("port", lambda v: is_int(v) and 1024 <= v <= 65535, "must be a port from 1024 to 65535")
    take("proxy_hops", lambda v: is_int(v) and 0 <= v <= 3, "must be 0 to 3")
    take("lead_handoff", lambda v: v in leads.HANDOFFS, "must be one of: " + ", ".join(leads.HANDOFFS))
    take("kanban_board", lambda v: isinstance(v, str) and (v == "" or leads.BOARD.fullmatch(v)),
         "must be a board slug")
    origin = raw.get("public_origin")
    if origin not in (None, ""):
        normalized = check_origin(origin) if isinstance(origin, str) else None
        if normalized:
            settings["public_origin"] = normalized
        else:
            errors.append("public_origin must be an https:// origin without a path")
    if settings["enabled"] and not settings["public_origin"]:
        errors.append("public_origin is required when enabled")
    return settings, errors


def load_sites(path):
    """``(sites, errors)`` from the sites file. A missing file means no sites."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except (OSError, ValueError):
        return [], ["sites.json is not valid JSON"]
    items = data.get("sites") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], ['sites.json must be {"sites": [...]}']
    sites, errors, seen = [], [], set()
    for index, site in enumerate(items[:MAX_SITES]):
        key = site.get("key") if isinstance(site, dict) else None
        if not isinstance(key, str) or not SITE_KEY.fullmatch(key):
            errors.append("site %d needs a key of lowercase letters, digits and dashes" % (index + 1))
        elif key in seen:
            errors.append("site key %s is used twice" % key)
        else:
            seen.add(key)
            sites.append(site)
    if len(items) > MAX_SITES:
        errors.append("only the first %d sites are used" % MAX_SITES)
    return sites, errors


def _get_secret(name):
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return ""
    return get_secret(name) or ""


def _error_code(error):
    try:
        return json.loads(error.read() or b"{}").get("code") or "errGeneric"
    except Exception:
        return "errGeneric"
    finally:
        error.close()


class Service:
    def __init__(self, home, runtime_factory, *, node=None, server=None, handoff=None,
                 secret=None, clock=time.monotonic):
        self.home = Path(home)
        self.data = self.home / "consult"
        self.runtime_factory = runtime_factory
        self.node = node if node is not None else shutil.which("node")
        self.server = Path(server) if server else ROOT / "dist" / "server.mjs"
        self.handoff = handoff or leads.to_kanban
        self.secret = secret or _get_secret
        self.clock = clock
        self.process = None
        self.token = ""
        self.base = ""
        self.signature = None
        self.settings = dict(DEFAULTS)
        self.connectors = {}
        self.applied = None
        self.errors = []
        self.site_errors = {}
        self.sites_file_errors = []
        self.lead_error = ""
        self.next_start = 0.0

    # Consultation server control API (loopback, in-memory token).
    def request(self, path, method="GET", body=None):
        request = Request(
            self.base + path,
            method=method,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None,
        )
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, settings):
        port = settings["port"]
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError("port %d is already in use" % port) from None
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token = secrets.token_urlsafe(32)
        self.base = "http://127.0.0.1:%d" % port
        # An allowlisted environment: the server never sees the engine's secrets.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TZ") if k in os.environ}
        env.update(
            PORT=str(port),
            PUBLIC_ORIGIN=settings["public_origin"],
            DB_PATH=str(self.data / "consult.sqlite"),
            CONSULT_CONTROL_TOKEN=self.token,
            CONSULT_PARENT_PID=str(os.getpid()),
            CONSULT_PROXY_HOPS=str(settings["proxy_hops"]),
        )
        challenge = self.secret(TURNSTILE_KEY)
        if challenge:
            env[TURNSTILE_KEY] = challenge
        self.process = subprocess.Popen(
            [self.node, str(self.server)], cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.process.poll() is not None:
                break
            try:
                self.request("/api/sites")
                return
            except (OSError, HTTPError):
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("the consultation server did not start")

    def stop(self):
        for connector, thread in self.connectors.values():
            connector.stop.set()
            connector.voice.shutdown()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for connector, thread in self.connectors.values():
            thread.join(timeout=2)
        self.connectors.clear()
        self.process = None
        self.token = ""
        self.applied = None

    def apply(self, sites):
        """Create or update each configured site, publish it, and pause sites no longer listed."""
        self.site_errors = {}
        existing = {}
        for row in self.request("/api/sites"):
            key = row["draft"].get("key")
            if key:
                existing[key] = row
        for site in sites:
            key = site["key"]
            try:
                if key in existing:
                    ident = existing[key]["id"]
                    self.request("/api/sites/" + ident, "PUT", site)
                else:
                    ident = self.request("/api/sites", "POST", site)["id"]
                self.request("/api/sites/%s/publish" % ident, "POST", {})
            except HTTPError as error:
                self.site_errors[key] = _error_code(error)
        listed = {site["key"] for site in sites}
        for key, row in existing.items():
            if key in listed or row["draft"].get("enabled") is False:
                continue
            try:
                self.request("/api/sites/" + row["id"], "PUT", {**row["draft"], "enabled": False})
                self.request("/api/sites/%s/publish" % row["id"], "POST", {})
            except HTTPError as error:
                self.site_errors[key] = _error_code(error)

    def sync_connectors(self):
        """One live connector per published site."""
        for site in self.request("/api/sites"):
            existing = self.connectors.get(site["id"])
            if (existing and existing[1].is_alive()) or not site["published"]:
                continue
            runtime = self.runtime_factory()
            if not runtime.available():
                raise RuntimeError("model access is unavailable to the plugin")
            connector = Connector(self.base, runtime, self.settings["voice_enabled"])
            try:
                code = self.request("/api/sites/%s/pair" % site["id"], "POST", {})["code"]
                connector.pair(code)
            except Exception:
                connector.pool.shutdown(wait=False, cancel_futures=True)
                raise
            context = contextvars.copy_context()
            thread = threading.Thread(target=lambda c=connector, x=context: x.run(c.run), daemon=True)
            self.connectors[site["id"]] = (connector, thread)
            thread.start()

    def hand_off_leads(self):
        if self.settings["lead_handoff"] != "kanban":
            return
        self.lead_error = ""
        for lead in self.request("/api/leads/pending"):
            try:
                ref = self.handoff(lead, self.settings["kanban_board"])
            except Exception as error:
                # The lead stays pending and is retried on the next tick.
                self.lead_error = "lead handoff failed: %s" % (str(error)[:200] or type(error).__name__)
                return
            try:
                self.request("/api/leads/%s/handed" % lead["id"], "POST", {"ref": "kanban:" + ref})
            except HTTPError as error:
                _error_code(error)

    def tick(self, raw_settings):
        settings, errors = parse_settings(raw_settings)
        if settings["enabled"] and not errors:
            if not self.node:
                errors.append("Node.js 24 or later is required")
            elif not self.server.is_file():
                errors.append("the built consultation server is missing")
        signature = tuple(settings[k] for k in ("public_origin", "port", "proxy_hops", "voice_enabled"))
        wanted = settings["enabled"] and not errors
        if self.process is not None and (not wanted or signature != self.signature or not self.running()):
            self.stop()
        self.settings = settings
        if wanted and self.process is None and self.clock() >= self.next_start:
            try:
                self.start(settings)
                self.signature = signature
            except RuntimeError as error:
                errors.append(str(error))
                self.next_start = self.clock() + 30
        if self.running():
            sites_file = self.data / "sites.json"
            try:
                digest = hashlib.sha256(sites_file.read_bytes()).hexdigest()
            except FileNotFoundError:
                digest = "missing"
            if digest != self.applied:
                sites, site_errors = load_sites(sites_file)
                self.apply(sites)
                self.sites_file_errors = site_errors
                self.applied = digest
            errors.extend(self.sites_file_errors)
            try:
                self.sync_connectors()
            except RuntimeError as error:
                errors.append(str(error))
            self.hand_off_leads()
            if self.lead_error:
                errors.append(self.lead_error)
        self.errors = errors
        self.write_status()

    def status(self):
        running = self.running()
        sites = []
        if running:
            try:
                for row in self.request("/api/sites"):
                    draft = row["draft"]
                    sites.append({
                        "key": draft.get("key", ""),
                        "id": row["id"],
                        "name": draft.get("name", ""),
                        "enabled": draft.get("enabled", True),
                        "published": row["published"],
                        "online": row["online"],
                        "voice": draft.get("engine") == "api" and bool(row["capabilities"].get("api")),
                        "snippet": row["snippet"],
                        "error": self.site_errors.get(draft.get("key", ""), ""),
                    })
            except (OSError, HTTPError):
                pass
        return {
            "enabled": self.settings["enabled"],
            "running": running,
            "publicOrigin": self.settings["public_origin"],
            "port": self.settings["port"],
            "voiceEnabled": self.settings["voice_enabled"],
            "leadHandoff": self.settings["lead_handoff"],
            "sites": sites,
            "errors": list(self.errors),
            "updated": int(time.time()),
        }

    def write_status(self):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.data / "status.json"
        temporary = target.with_name(".status.json.tmp")
        temporary.write_text(json.dumps(self.status(), indent=2), encoding="utf-8")
        temporary.chmod(0o640)
        os.replace(temporary, target)


def serve(read_settings, home, runtime_factory, interval=5.0, stop=None, **options):
    """Run until SIGTERM/SIGINT (or ``stop`` is set), applying configuration every ``interval``."""
    service = Service(home, runtime_factory, **options)
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    try:
        while True:
            try:
                service.tick(read_settings())
            except Exception as error:
                service.errors = ["service error: %s" % type(error).__name__]
                try:
                    service.write_status()
                except OSError:
                    pass
            if stop.wait(interval):
                break
    finally:
        service.stop()
        service.write_status()
    return service
