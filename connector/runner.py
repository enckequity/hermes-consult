"""Long-poll connector between the local consultation server and the workspace model.

The service pairs one connector per published site over loopback; the pairing credential
stays in memory.
"""

import contextvars
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from urllib.error import HTTPError
from .voice import VoiceManager, VoiceServiceError


class Connector:
    def __init__(self, url, runtime, voice_enabled=False):
        parsed = urlparse(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        ):
            raise ValueError("HTTPS relay required")
        if parsed.username or parsed.password or parsed.path not in ("", "/"):
            raise ValueError("Relay origin only")
        self.url = url.rstrip("/")
        self.token = ""
        self.runtime = runtime
        self.voice = VoiceManager(
            runtime,
            voice_enabled,
            lambda session: self.request("/connector/question", {"sessionId": session}),
        )
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.results = {}
        self.lock = threading.Lock()
        self.inflight = set()

    def request(self, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = Request(
            self.url + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    def pair(self, code):
        result = self.request("/connector/pair", {"code": code})
        self.token = result["token"]

    def run(self):
        def maintain():
            while not self.stop.wait(8):
                self.voice.reap()
                try:
                    self.request(
                        "/connector/heartbeat",
                        {"capabilities": self.voice.capabilities()},
                    )
                except HTTPError as e:
                    if e.code == 401:
                        self.stop.set()
                        self.voice.shutdown()
                except Exception:
                    pass

        self.request(
            "/connector/heartbeat", {"capabilities": self.voice.capabilities()}
        )
        context = contextvars.copy_context()
        threading.Thread(target=lambda: context.run(maintain), daemon=True).start()
        try:
            while not self.stop.is_set():
                try:
                    job = self.request("/connector/jobs")
                    if job.get("idle"):
                        continue
                    with self.lock:
                        if len(self.inflight) >= 12:
                            raise RuntimeError("Connector queue full")
                        self.inflight.add(job["id"])
                    self.pool.submit(contextvars.copy_context().run, self.execute, job)
                except HTTPError as e:
                    if e.code == 401:
                        break
                    self.stop.wait(3)
                except Exception:
                    self.stop.wait(3)
        finally:
            self.stop.set()
            self.voice.shutdown()
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.token = ""

    def execute(self, job):
        ident = job["id"]
        try:
            if ident in self.results:
                result = self.results[ident]
            else:
                if job["deadline"] < time.time() * 1000:
                    raise RuntimeError("Expired job")
                kind = job["kind"]
                sid = job["sessionId"]
                payload = job["payload"]
                if kind == "ask":
                    data = self.runtime.ask(sid, payload)
                elif kind == "start":
                    data = self.voice.start(sid, payload)
                elif kind == "close":
                    data = self.voice.close(sid)
                elif kind == "heartbeat":
                    data = self.voice.heartbeat(sid)
                else:
                    raise RuntimeError("Unsupported job")
                result = {"id": ident, "ok": True, "data": data}
        except Exception as error:
            result = {
                "id": ident,
                "ok": False,
                "error": "Connector operation failed; check configured model access and provider account.",
                "errorCode": error.code
                if isinstance(error, VoiceServiceError)
                else "connector_failed",
            }
        self.results[ident] = result
        if len(self.results) > 256:
            self.results.pop(next(iter(self.results)))
        for attempt in range(3):
            try:
                self.request("/connector/result", result)
                break
            except Exception:
                self.stop.wait(1)
        with self.lock:
            self.inflight.discard(ident)
