"""A real local HTTP server standing in for the OpenAI Responses API (docs/specs/llm-adapter.md).

Replies are scripted per test; every request's path, headers and body are recorded.
No test reaches the internet.
"""

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import JsonValue

LOOPBACK = "127.0.0.1"
DEFAULT_WAIT_SECONDS = 3.0
POLL_SECONDS = 0.01


@dataclass(frozen=True)
class ScriptedReply:
    status: int = 200
    json_body: JsonValue | None = None
    raw_body: bytes | None = None
    delay_seconds: float = 0.0
    drop_connection: bool = False
    """Close the socket without writing any response (a transport error for the client)."""
    drip_bytes: int = 0
    """When > 0: send the status line and headers with `Content-Length: drip_bytes`, then write one byte
    (a space) every `drip_interval_seconds` until `drip_bytes` are sent. Bodies are ignored."""
    drip_interval_seconds: float = 0.0


@dataclass(frozen=True)
class RecordedRequest:
    path: str
    headers: dict[str, str]
    """Header names lower-cased."""
    raw_body: bytes

    def json_object(self) -> dict[str, JsonValue]:
        parsed: JsonValue = json.loads(self.raw_body)
        if not isinstance(parsed, dict):
            raise AssertionError(f"request body is not a JSON object: {self.raw_body!r}")
        return parsed

    def string_field(self, name: str) -> str:
        value = self.json_object()[name]
        if not isinstance(value, str):
            raise AssertionError(f"request body field {name!r} is not a string: {value!r}")
        return value


def completed_reply(text: str, model: str) -> ScriptedReply:
    return ScriptedReply(
        json_body={
            "id": "resp_fake",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
            ],
        }
    )


class FakeOpenAiServer:
    """Serves `replies` in order, one per request; the last reply repeats once the script is used up."""

    def __init__(self, replies: Sequence[ScriptedReply]) -> None:
        self._replies = list(replies) or [ScriptedReply(status=500)]
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._server = ThreadingHTTPServer((LOOPBACK, 0), self._make_handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self._server.server_address[1]}/v1"

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    def models(self) -> list[str]:
        return [request.string_field("model") for request in self.requests]

    def wait_for_request_count(self, count: int, timeout_seconds: float = DEFAULT_WAIT_SECONDS) -> None:
        deadline = time.monotonic() + timeout_seconds
        while len(self.requests) < count:
            if time.monotonic() > deadline:
                raise AssertionError(f"expected {count} requests, saw {len(self.requests)}")
            time.sleep(POLL_SECONDS)

    def close(self) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=DEFAULT_WAIT_SECONDS)

    def _next_reply(self, request: RecordedRequest) -> ScriptedReply:
        with self._lock:
            index = len(self._requests)
            self._requests.append(request)
        return self._replies[min(index, len(self._replies) - 1)]

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                request = RecordedRequest(
                    path=self.path,
                    headers={name.lower(): value for name, value in self.headers.items()},
                    raw_body=self.rfile.read(length),
                )
                reply = owner._next_reply(request)
                if reply.drop_connection:
                    self.close_connection = True
                    return
                if owner._stop.wait(reply.delay_seconds):
                    return
                if reply.drip_bytes > 0:
                    self._drip(reply)
                    return
                payload = (
                    reply.raw_body if reply.raw_body is not None else json.dumps(reply.json_body).encode()
                )
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _drip(self, reply: ScriptedReply) -> None:
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(reply.drip_bytes))
                    self.end_headers()
                    self.wfile.flush()
                    for _ in range(reply.drip_bytes):
                        if owner._stop.wait(reply.drip_interval_seconds):
                            return
                        self.wfile.write(b" ")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def fake_server_session() -> Iterator[Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]]:
    """Body of a pytest fixture: yields a starter, closes every started server on teardown."""
    started: list[FakeOpenAiServer] = []

    def start(replies: Sequence[ScriptedReply]) -> FakeOpenAiServer:
        server = FakeOpenAiServer(replies)
        started.append(server)
        return server

    try:
        yield start
    finally:
        for server in started:
            server.close()


def closed_port_base_url() -> str:
    """A loopback base URL nothing listens on (connection refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((LOOPBACK, 0))
        port = probe.getsockname()[1]
    return f"http://{LOOPBACK}:{port}/v1"
