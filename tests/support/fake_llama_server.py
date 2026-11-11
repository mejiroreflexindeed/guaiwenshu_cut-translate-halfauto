"""A loopback stand-in for llama-server used by the W04 translation tests (T04: real HTTP, no real model).

Started with the legacy argument list (``-m MODEL -ngl N --port P --host H --ctx-size C --parallel 1``). Its behaviour
is scripted by ``<MODEL>.behaviour.json`` (read at every request, so a test can change it between runs):

    {"startup_delay": 0.0, "exit_on_start": false, "model_id": null, "chunk": 7,
     "responses": [ {spec}, ... ],      # consumed in order, one per POST /v1/chat/completions
     "default": "echo"}                 # what happens when the list is exhausted

Response specs: {"kind": "echo"} (one SSE line "译:<source line>" per source line, finish=stop, [DONE]),
{"kind": "echo_lines", "lines": k} (k lines whatever the input), {"kind": "sse", "content": str, "finish": "stop"|"length"|null,
"done": bool}, {"kind": "json", "content": str}, {"kind": "http_error", "status": 400, "body": {...}}, {"kind": "drop"} (headers and a
partial chunk, then the connection is closed), {"kind": "stall", "content_before": str, "seconds": n} (one chunk, then silence).
Every request is appended to ``<MODEL>.requests.jsonl``.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BODY_MARKER = "将下面的文本从日文翻译成简体中文：\n"


def parse_args(argv: list[str]) -> dict:
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        if argv[index].startswith("-") and index + 1 < len(argv):
            values[argv[index]] = argv[index + 1]
            index += 2
        else:
            index += 1
    return values


def main() -> int:
    options = parse_args(sys.argv[1:])
    model = Path(options["-m"])
    port = int(options["--port"])
    host = options.get("--host", "127.0.0.1")
    behaviour_path = model.with_suffix(".behaviour.json")
    log_path = model.with_suffix(".requests.jsonl")

    def behaviour() -> dict:
        try:
            return json.loads(behaviour_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    start = behaviour()
    if start.get("exit_on_start"):
        print("fake llama-server: exiting on start as scripted", flush=True)
        return 3
    time.sleep(float(start.get("startup_delay", 0.0)))
    counter = {"served": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _json(self, status: int, value: dict) -> None:
            body = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def do_GET(self):
            current = behaviour()
            if self.path == "/health":
                self._json(200, {"status": "ok"})
            elif self.path == "/v1/models":
                self._json(200, {"object": "list", "data": [{"id": current.get("model_id") or str(model), "object": "model"}]})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            current = behaviour()
            with lock:
                position = counter["served"]
                counter["served"] += 1
            responses = current.get("responses", [])
            spec = responses[position] if position < len(responses) else {"kind": current.get("default", "echo")}
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"position": position, "path": self.path, "spec": spec, "payload": payload}, ensure_ascii=False) + "\n")
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": "not found"})
                return
            kind = spec.get("kind", "echo")
            user_body = payload["messages"][-1]["content"]
            source_lines = user_body.split(BODY_MARKER, 1)[1].split("\n") if BODY_MARKER in user_body else [user_body]
            chunk = int(current.get("chunk", 7))
            if kind == "echo":
                self._sse(["译:" + line for line in source_lines], finish="stop", done=True, chunk=chunk)
            elif kind == "echo_lines":
                self._sse([f"译文行{n + 1}" for n in range(int(spec["lines"]))], finish="stop", done=True, chunk=chunk)
            elif kind == "sse":
                self._sse(str(spec.get("content", "")).split("\n"), finish=spec.get("finish", "stop"), done=bool(spec.get("done", True)), chunk=chunk)
            elif kind == "json":
                self._json(200, {"choices": [{"message": {"content": spec.get("content", "")}, "finish_reason": spec.get("finish", "stop")}]})
            elif kind == "http_error":
                self._json(int(spec.get("status", 500)), spec.get("body", {"error": {"message": "scripted error"}}))
            elif kind == "drop":
                self._sse_headers()
                self._chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "半句"}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)  # the TCP connection drops here: no terminating chunk, no [DONE]
                except OSError:
                    pass
            elif kind == "stall":
                self._sse_headers()
                self._chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": spec.get("content_before", "部分")}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
                self.wfile.flush()
                deadline = time.monotonic() + float(spec.get("seconds", 30))
                while time.monotonic() < deadline:
                    time.sleep(0.05)
                try:
                    self._sse_tail(["终于完成"], finish="stop", done=True, chunk=chunk)
                except OSError:
                    pass
            else:
                self._json(500, {"error": f"unknown scripted kind {kind!r}"})

        def _sse_headers(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

        def _chunk(self, text: str) -> None:
            data = text.encode("utf-8")
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")

        def _sse_tail(self, lines: list[str], *, finish, done: bool, chunk: int) -> None:
            content = "\n".join(lines)
            for start in range(0, len(content), chunk):          # arbitrary split points inside UTF-8 text, like a real socket
                piece = content[start:start + chunk]
                self._chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n")
                self.wfile.flush()
            if finish is not None:
                self._chunk("data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}) + "\n\n")
            if done:
                self._chunk("data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _sse(self, lines: list[str], *, finish, done: bool, chunk: int) -> None:
            self._sse_headers()
            self._sse_tail(lines, finish=finish, done=done, chunk=chunk)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"fake llama-server listening on {host}:{port} model={model}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
