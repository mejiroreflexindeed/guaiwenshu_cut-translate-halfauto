#!/usr/bin/env python3
"""Translation: the existing Sakura text protocol behind one Interface (plan 1.1, sections 6.5, 6.8, 8.4, 8.5; W04).

Interface (fixed by the plan):
    translate(TranslationRequest, runtime=...) -> {translations_ref, checkpoint_ref, raw_exchange_refs, issues}

Kept from the legacy adapter (``voicetransl_adapter.translate`` / ``sakura_client``): the plain-text Sakura prompt
(``plain_translation_payload``), the lossless SSE reader (``read_chat_stream``), the batch rules (a multi-line cue is
its own request; single-line cues go in batches; a response is mapped only when it is complete and has exactly one
line per cue; anything else is split into per-cue requests with the whole batch as reference), at most three attempts
per unit (first + two retries) with no outer multiplication, the pre/glossary/post dictionary handling and the
term/kana/uncertainty review flags. Nothing is appended to a translation to satisfy a missing term.

What changes: the model server is a process this program owns (started through the owned process runner, stopped
when the stage ends; a foreign listener on the port is SERVER_PORT_CONFLICT and is never adopted), connection
timeout is 10 s with no generation deadline (an idle stream only raises a waiting event), every cue is checkpointed
as soon as it is mapped so a resumed attempt repeats no request, context overflow is CONTEXT_EXCEEDED with the full
pending text saved, and the result is expressed as TranslationRecord entries with request/response evidence.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from upgrade import dictionary
from upgrade.contracts import ContractError, SCHEMA_VERSION, canonical_json, load_record, parse_json_text, require_record, sha256_bytes, sha256_text, signature, utc_now
from upgrade.process_runner import CancelToken, ProcessHandle, process_spec
from upgrade.storage import artifact_ref, atomic_write_text, render_json, safe_join

TRANSLATION_ADAPTER_VERSION = 1
PROTOCOL_ID = "sakura_plain_text_v1"
MAX_ATTEMPTS = 3                      # first attempt + two retries (plan 8.4); never multiplied by an outer loop
RETRY_PAUSE_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 10.0
STALL_NOTICE_SECONDS = 60.0
HEALTH_POLL_SECONDS = 2.0
SERVER_STOP_GRACE_SECONDS = 0.0       # normal stop: the legacy client terminated its server immediately
DEFAULT_ENDPOINT = "http://127.0.0.1:8989"
PARAMETER_DEFAULTS = {"batch_size": 10, "context_size": 16384, "gpu_layers": 100, "temperature": 0.3, "top_p": 0.8, "workers": 1,
                      "use_native_dictionaries": True, "native_prompt_mode": "不使用"}
EventSink = Callable[[dict], None]


@dataclass
class TranslationRuntime:
    """Runtime handles injected by the runner (plan 6.8)."""
    project_dir: Path
    data_root: Path
    output_dir: Path                    # stage attempt directory; refs in the result are relative to it
    resources: dict[str, dict]          # translation_engine, translation_model
    owner_token: str
    cancel: CancelToken = field(default_factory=CancelToken)
    emit: EventSink = lambda event: None
    capability_id: str = "verified_existing"


class TranslationFailure(Exception):
    """One attempt failed; ``code`` is the error code to use when the attempts are exhausted."""

    def __init__(self, message: str, *, code: str = "PROCESS_FAILED", kind: str = "request_failed", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.kind = kind
        self.details = details or {}


# --------------------------------------------------------------------------- parameters

def effective_parameters(settings: dict) -> dict:
    """Every parameter that reaches the server or the prompt, with the locally verified defaults (plan 8.4)."""
    params: dict[str, Any] = {}
    for key, default in PARAMETER_DEFAULTS.items():
        value = settings.get(key, default)
        if key in ("batch_size", "context_size", "gpu_layers", "workers"):
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"translation.{key} must be an integer", details={"value": value}) from exc
        elif key in ("temperature", "top_p"):
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise ContractError("CONFIG_UNSUPPORTED_COMMAND", f"translation.{key} must be a number", details={"value": value}) from exc
        elif key == "use_native_dictionaries":
            value = bool(value)
        params[key] = value
    if params["batch_size"] <= 0:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "translation.batch_size must be positive", details={"value": params["batch_size"]})
    if params["context_size"] < 0:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "translation.context_size must be nonnegative (0 uses the model default)", details={"value": params["context_size"]})
    params["native_prompt_mode"] = dictionary.prompt_mode(settings)
    params["endpoint"] = str(settings.get("endpoint") or DEFAULT_ENDPOINT).rstrip("/")
    parsed = urllib.parse.urlsplit(params["endpoint"])
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port:
        raise ContractError("CONFIG_UNSUPPORTED_COMMAND", "the model server endpoint must be a loopback http address with a port; external services are not a default backend",
                            details={"endpoint": params["endpoint"]})
    params["workers_effective"] = 1     # legacy: --parallel 1 and sequential batches; other values are recorded, not executed
    return params


def server_argv(*, model: Path, params: dict) -> list[str]:
    """The legacy llama-server command line, unchanged (sakura_client.start_server)."""
    port = urllib.parse.urlsplit(params["endpoint"]).port
    return ["-m", str(model), "-ngl", str(params["gpu_layers"]), "--port", str(port), "--host", "127.0.0.1", "--ctx-size", str(params["context_size"]), "--parallel", "1"]


def launch_spec(executable: Path, argv: list[str]) -> tuple[Path, list[str]]:
    """The process launch seam: what is actually started for the model server (tests substitute a loopback double)."""
    return executable, argv


def _resource_file(resource: dict, names: tuple[str, ...]) -> Path:
    root = Path(resource["absolute_path"])
    if root.is_file():
        return root
    for name in names:
        if (root / name).is_file():
            return root / name
    raise ContractError("RESOURCE_MISSING", f"none of {list(names)} exists in resource {resource['resource_ref']['resource_id']!r}",
                        details={"resource_id": resource["resource_ref"]["resource_id"]})


# --------------------------------------------------------------------------- owned model server

def _port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _http_get_json(url: str, timeout: float = 3.0) -> dict | None:
    parsed = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request("GET", parsed.path or "/")
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            return None
        value = json.loads(body.decode("utf-8"))
        return value if isinstance(value, dict) else {"value": value}
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        connection.close()


class OwnedServer:
    """llama-server started, health-checked and stopped by this program (plan 8.4)."""

    def __init__(self, *, executable: Path, model: Path, params: dict, log_dir: Path, runtime: TranslationRuntime, engine_ref: dict, model_ref: dict,
                 startup_timeout: float | None):
        self.executable, self.model, self.params, self.log_dir, self.runtime = executable, model, params, log_dir, runtime
        self.engine_ref, self.model_ref, self.startup_timeout = engine_ref, model_ref, startup_timeout
        self.endpoint = params["endpoint"]
        parsed = urllib.parse.urlsplit(self.endpoint)
        self.host, self.port = parsed.hostname or "127.0.0.1", int(parsed.port)
        self.handle: ProcessHandle | None = None
        self.owner: dict[str, Any] = {}

    def __enter__(self) -> "OwnedServer":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop("translation_stage_finished" if exc_info[0] is None else "translation_stage_aborted")

    def start(self) -> None:
        if _port_listening(self.host, self.port):
            raise ContractError("SERVER_PORT_CONFLICT", f"another service already listens on {self.host}:{self.port}; it is not adopted and not stopped. "
                                "Stop it or configure another loopback port", details={"endpoint": self.endpoint})
        executable, argv = launch_spec(self.executable, server_argv(model=self.model, params=self.params))
        spec = process_spec(executable, argv, cwd=Path(self.executable).parent, log_dir=self.log_dir, owner_token=self.runtime.owner_token,
                            capability_id=self.runtime.capability_id)
        self.handle = ProcessHandle(spec, cancel=None, event_sink=self.runtime.emit).start()
        self.owner = {"schema_version": SCHEMA_VERSION, "pid": self.handle.process.pid, "started_at": self.handle.started_at, "nonce": self.runtime.owner_token,
                      "engine_ref": self.engine_ref, "model_ref": self.model_ref, "endpoint": self.endpoint, "argv": [str(executable), *argv],
                      "context_size": self.params["context_size"], "gpu_layers": self.params["gpu_layers"], "state": "STARTING"}
        self._write_owner()
        try:
            self._wait_ready()
        except BaseException:
            self.stop("startup_failed")
            raise
        self.owner.update(state="READY", ready_at=utc_now())
        self._write_owner()
        self.runtime.emit({"event": "model_server_ready", "endpoint": self.endpoint, "pid": self.owner["pid"]})

    def _write_owner(self) -> None:
        atomic_write_text(self.log_dir / "server_owner.json", render_json(self.owner))

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout if self.startup_timeout else None
        while True:
            if self.runtime.cancel.is_set():
                raise ContractError("PROCESS_CANCELLED", "cancelled while the model server was loading", retryable=True)
            code = self.handle.poll()
            if code is not None:
                raise ContractError("PROCESS_FAILED", f"the model server exited with code {code} before it became ready", details={"exit_code": code, "log_dir": str(self.log_dir)})
            if _http_get_json(self.endpoint + "/health") is not None:
                break
            if deadline is not None and time.monotonic() > deadline:
                raise ContractError("PROCESS_FAILED", f"the model server was not ready within {self.startup_timeout} seconds", details={"endpoint": self.endpoint})
            self.runtime.cancel.wait(HEALTH_POLL_SECONDS)
        models = _http_get_json(self.endpoint + "/v1/models") or {}
        ids = [str(item.get("id", "")) for item in models.get("data", []) if isinstance(item, dict)]
        basename = lambda value: value.replace("\\", "/").rsplit("/", 1)[-1].casefold()  # noqa: E731
        if not any(basename(item) == self.model.name.casefold() for item in ids):
            raise ContractError("SERVER_PORT_CONFLICT", f"the service answering on {self.endpoint} does not serve {self.model.name}; it is not used",
                                details={"endpoint": self.endpoint, "reported_models": ids, "expected": self.model.name})
        self.owner["reported_models"] = ids

    def stop(self, reason: str) -> None:
        if self.handle is None or self.handle.process is None:
            return
        if self.handle.process.poll() is None:
            self.handle.terminate(reason=reason, grace_seconds=SERVER_STOP_GRACE_SECONDS)
        result = self.handle.wait()
        self.owner.update(state="STOPPED", stopped_at=utc_now(), stop_reason=reason, exit_code=result["exit_code"])
        self._write_owner()
        self.handle = None


# --------------------------------------------------------------------------- transport (10 s connect, no generation deadline, cancellable)

class Transport:
    """One request at a time against the owned server. A watcher thread closes the socket on cancel and raises a
    waiting event when the stream has been silent for STALL_NOTICE_SECONDS; it never terminates the request."""

    def __init__(self, runtime: TranslationRuntime):
        self.runtime = runtime
        self.lock = threading.Lock()
        self.connection: http.client.HTTPConnection | None = None
        self.raw_socket: socket.socket | None = None        # the OS socket of the running request; shut down from the watcher on cancel
        self.last_activity = time.monotonic()
        self.stall_notified = False
        self.cancelled = False
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self) -> "Transport":
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _watch(self) -> None:
        while not self.stop_event.wait(0.25):
            with self.lock:
                raw_socket = self.raw_socket
                idle = time.monotonic() - self.last_activity
                if raw_socket is not None and self.runtime.cancel.is_set():
                    self.cancelled = True
                    self.raw_socket = None
                    self._abort(raw_socket)        # unblocks the reader thread, which then raises PROCESS_CANCELLED and closes the connection
                    continue
            if raw_socket is not None and idle >= STALL_NOTICE_SECONDS and not self.stall_notified:
                self.stall_notified = True
                self.runtime.emit({"event": "translation_waiting", "idle_seconds": round(idle, 1), "note": "no new content from the model; waiting, not terminating; cancel is available"})

    @staticmethod
    def _abort(raw_socket: socket.socket) -> None:
        """Abort a blocking read from another thread. On Windows ``shutdown`` alone does not wake a pending recv, so the OS
        handle is closed as well (the Python object is detached first, so the reader's own close() stays harmless)."""
        try:
            raw_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            handle = raw_socket.detach()
            if handle != -1:
                socket.socket(fileno=handle).close()
        except OSError:
            pass

    @staticmethod
    def _close(connection: http.client.HTTPConnection) -> None:
        try:
            connection.close()
        except OSError:
            pass

    def touch(self) -> None:
        with self.lock:
            self.last_activity = time.monotonic()
            self.stall_notified = False

    def post_json(self, url: str, payload: dict, *, exchange_dir: Path, on_stream: Callable[[str], None] | None = None) -> dict:
        """POST JSON, save request/response bytes under ``exchange_dir``, return the parsed chat response.

        Completeness is checked the legacy way: SSE must end with [DONE] after a finish_reason; a non-stream body must
        be complete JSON; any HTTP status other than 200 is a failure with the body saved.
        """
        from sakura_client import read_chat_stream
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        parsed = urllib.parse.urlsplit(url)
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / "request.json").write_bytes(data)
        metadata: dict[str, Any] = {"started_at": utc_now(), "url": url, "method": "POST", "request_bytes": len(data), "request_sha256": sha256_bytes(data),
                                    "connect_timeout_seconds": CONNECT_TIMEOUT_SECONDS, "generation_deadline": None, "state": "REQUESTED"}
        atomic_write_text(exchange_dir / "exchange.json", render_json(metadata))
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=CONNECT_TIMEOUT_SECONDS)
        if self.runtime.cancel.is_set():
            raise ContractError("PROCESS_CANCELLED", "cancelled before the translation request was sent", retryable=True)
        try:
            connection.connect()
            connection.sock.settimeout(None)
            with self.lock:
                self.connection = connection
                self.raw_socket = connection.sock
                self.last_activity = time.monotonic()
                self.stall_notified = False
            connection.request("POST", parsed.path or "/", body=data, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            response = connection.getresponse()
            content_type = response.headers.get("Content-Type", "")
            metadata.update(status=response.status, content_type=content_type, state="RECEIVING")
            if response.status != 200:
                body = response.read()
                (exchange_dir / "response.body").write_bytes(body)
                metadata.update(state="FAILED", response_bytes=len(body), response_sha256=sha256_bytes(body))
                raise TranslationFailure(f"HTTP {response.status}", code=_code_for_http_error(response.status, body), kind=f"http_{response.status}",
                                         details={"status": response.status, "body_preview": body[:500].decode("utf-8", errors="replace")})
            if "text/event-stream" in content_type.lower():
                def forward(content: str) -> None:
                    self.touch()
                    if on_stream is not None:
                        on_stream(content)
                result = read_chat_stream(_Touching(response, self.touch), exchange_dir, metadata, forward)
            else:
                body = response.read()
                (exchange_dir / "response.body").write_bytes(body)
                metadata.update(response_bytes=len(body), response_sha256=sha256_bytes(body))
                result = json.loads(body.decode("utf-8"))
            metadata["state"] = "PARSED"
            return result
        except (TranslationFailure, ContractError):
            raise
        except Exception as exc:  # noqa: BLE001 - socket, HTTP, decoding or closed-connection failures of this one attempt
            metadata.update(state="FAILED", error_type=type(exc).__name__, error=str(exc))
            if self.cancelled or self.runtime.cancel.is_set():
                raise ContractError("PROCESS_CANCELLED", "translation request cancelled; the partial response is kept as evidence only", retryable=True) from exc
            raise TranslationFailure(f"{type(exc).__name__}: {exc}", kind="incomplete_response") from exc
        finally:
            with self.lock:
                self.connection = None
                self.raw_socket = None
            self._close(connection)
            metadata["finished_at"] = utc_now()
            atomic_write_text(exchange_dir / "exchange.json", render_json(metadata))


class _Touching:
    """Line iterator over the HTTP response that reports activity to the transport (bytes received, not only tokens)."""

    def __init__(self, response, touch: Callable[[], None]):
        self.response = response
        self.touch = touch
        self.status = response.status
        self.headers = response.headers

    def __iter__(self):
        for line in self.response:
            self.touch()
            yield line


def _code_for_http_error(status: int, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").lower()
    if status == 400 and ("exceed_context_size" in text or ("context" in text and ("exceed" in text or "too large" in text or "too long" in text))):
        return "CONTEXT_EXCEEDED"
    return "PROCESS_FAILED"


# --------------------------------------------------------------------------- checkpoint

class Checkpoint:
    """Per-cue checkpoint files under ``<checkpoint_dir>/cues``; exchanges under ``<checkpoint_dir>/exchanges``."""

    def __init__(self, directory: Path, translation_signature: str):
        self.dir = Path(directory)
        self.signature = translation_signature
        (self.dir / "cues").mkdir(parents=True, exist_ok=True)
        (self.dir / "exchanges").mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.dir / "checkpoint.json", render_json({"schema_version": SCHEMA_VERSION, "signature": translation_signature, "updated_at": utc_now()}))

    def completed(self, segments: list[dict]) -> dict[str, dict]:
        done: dict[str, dict] = {}
        for segment in segments:
            path = self.dir / "cues" / f"{segment['cue_id']}.json"
            if not path.is_file():
                continue
            try:
                entry = parse_json_text(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(entry, dict) and entry.get("signature") == self.signature and entry.get("source_text_sha256") == sha256_text(segment["source_text"]) \
                    and isinstance(entry.get("raw_text"), str) and entry["raw_text"].strip():
                done[segment["cue_id"]] = entry
        return done

    def new_exchange_dir(self) -> Path:
        existing = [p for p in (self.dir / "exchanges").iterdir() if p.is_dir()]
        return self.dir / "exchanges" / f"{len(existing) + 1:06d}"

    def commit(self, cue: dict, raw_text: str, *, exchange_dir: Path, mapping: str, prompt_sha256: str) -> dict:
        entry = {"schema_version": SCHEMA_VERSION, "signature": self.signature, "cue_id": cue["cue_id"], "index": cue["index"],
                 "source_text_sha256": sha256_text(cue["source_text"]), "raw_text": raw_text, "exchange": exchange_dir.relative_to(self.dir).as_posix(),
                 "mapping": mapping, "prompt_sha256": prompt_sha256, "completed_at": utc_now()}
        atomic_write_text(self.dir / "cues" / f"{cue['cue_id']}.json", render_json(entry))
        return entry


# --------------------------------------------------------------------------- batches (legacy rules)

def _units(batch: list[dict]) -> list[list[dict]]:
    """A multi-line cue is one request on its own; consecutive single-line cues share one request (sakura_client.translate_batch)."""
    units: list[list[dict]] = []
    current: list[dict] = []
    for item in batch:
        if "\n" in str(item["text"]) or "\r" in str(item["text"]):
            if current:
                units.append(current)
                current = []
            units.append([item])
        else:
            current.append(item)
    if current:
        units.append(current)
    return units


def _translate_batch(batch: list[dict], *, by_id: dict[int, dict], transport: Transport, checkpoint: Checkpoint, endpoint: str, model_name: str, system_prompt: str,
                     params: dict, glossary_text: str, runtime: TranslationRuntime, stats: dict) -> None:
    """Translate one batch with the legacy unit/retry rules; mapped cues are checkpointed immediately."""
    from sakura_client import plain_translation_payload
    units = _units(batch)
    url = endpoint + "/v1/chat/completions"
    while units:
        items = units.pop(0)
        payload = plain_translation_payload(model_name, system_prompt, items, batch, temperature=params["temperature"], top_p=params["top_p"], glossary_text=glossary_text)
        prompt_sha256 = sha256_text(canonical_json(payload["messages"]))
        last: TranslationFailure | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if runtime.cancel.is_set():
                raise ContractError("PROCESS_CANCELLED", "translation cancelled between requests", retryable=True)
            exchange_dir = checkpoint.new_exchange_dir()
            record: dict[str, Any] = {"started_at": utc_now(), "items": items, "reference": batch, "attempt": attempt, "max_attempts": MAX_ATTEMPTS, "state": "REQUESTED",
                                      "prompt_sha256": prompt_sha256}
            exchange_dir.mkdir(parents=True)
            atomic_write_text(exchange_dir / "translation.json", render_json(record))
            stats["requests"] += 1
            runtime.emit({"event": "translation_request", "ids": [item["id"] for item in items], "attempt": attempt, "exchange": exchange_dir.name})
            try:
                response = transport.post_json(url, payload, exchange_dir=exchange_dir)
                choice = response["choices"][0]
                record["finish_reason"] = choice.get("finish_reason")
                if choice.get("finish_reason") == "length":
                    raise TranslationFailure("model response was truncated by its generation limit", code="CONTEXT_EXCEEDED", kind="generation_truncated")
                content = choice["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise TranslationFailure("model response did not contain translation text", kind="empty_response")
                text = content.strip()
                lines = text.splitlines()
                if len(items) == 1:
                    parsed = {int(items[0]["id"]): text}
                    record["mapping"] = "single_cue_complete_body"
                elif len(lines) == len(items) and all(line.strip() for line in lines):
                    parsed = {int(item["id"]): line.strip() for item, line in zip(items, lines)}
                    record["mapping"] = "input_line_order_not_semantic_verification"
                else:
                    # plan 8.4: the raw response is saved; the unit is split into per-cue requests; no line is guessed
                    record.update(state="AMBIGUOUS_LINES", line_count=len(lines), expected_lines=len(items))
                    stats["ambiguous_splits"] += 1
                    units = [[item] for item in items] + units
                    break
                for item_id, raw_text in parsed.items():
                    checkpoint.commit(by_id[item_id], raw_text, exchange_dir=exchange_dir, mapping=record["mapping"], prompt_sha256=prompt_sha256)
                record.update(state="MAPPED", translations={str(key): value for key, value in parsed.items()})
                runtime.emit({"event": "translation_mapped", "ids": sorted(parsed), "mapping": record["mapping"]})
                break
            except TranslationFailure as exc:
                last = exc
                record.update(state="FAILED", error_type=type(exc).__name__, error=str(exc), code=exc.code, kind=exc.kind)
                if exc.code == "CONTEXT_EXCEEDED" and exc.kind != "generation_truncated":
                    break                                   # a request the server cannot fit will not fit on a retry either
                if attempt < MAX_ATTEMPTS:
                    runtime.cancel.wait(RETRY_PAUSE_SECONDS)
            except (KeyError, IndexError, TypeError) as exc:
                last = TranslationFailure(f"model response has an unexpected structure: {type(exc).__name__}: {exc}", kind="unexpected_structure")
                record.update(state="FAILED", error_type=type(exc).__name__, error=str(exc), code="PROCESS_FAILED", kind="unexpected_structure")
                if attempt < MAX_ATTEMPTS:
                    runtime.cancel.wait(RETRY_PAUSE_SECONDS)
            finally:
                record["finished_at"] = utc_now()
                atomic_write_text(exchange_dir / "translation.json", render_json(record))
        if record.get("state") == "FAILED":
            assert last is not None
            raise _stage_stop(items, last, glossary_text=glossary_text, attempts=record["attempt"])


def _stage_stop(items: list[dict], last: TranslationFailure, *, glossary_text: str, attempts: int) -> ContractError:
    """The error that stops the TRANSLATION stage; completed cues stay checkpointed and the pending text is saved whole."""
    pending_text = [{"id": item["id"], "cue_id": item["cue_id"], "text": item["text"]} for item in items]
    ids = [item["id"] for item in items]
    if last.code == "CONTEXT_EXCEEDED":
        message = (f"the model server cannot fit the request for cue(s) {ids} ({last}). The complete pending text is saved; lower the batch size or revise the "
                   "source segmentation in a new task")
    else:
        message = f"translation of cue(s) {ids} failed after {attempts} attempt(s): {last}; completed cues stay checkpointed, nothing partial is committed"
    return ContractError(last.code, message, retryable=last.code != "CONTEXT_EXCEEDED",
                         details={"kind": last.kind, "attempts": attempts, "max_attempts": MAX_ATTEMPTS, "pending_items": pending_text,
                                  "glossary_lines": glossary_text.count("\n") + 1 if glossary_text else 0, **last.details})


# --------------------------------------------------------------------------- post-processing (legacy functions, traced)

def _apply_traced(text: str, entries: list) -> tuple[str, list[dict]]:
    from voice_dictionary import apply_native_dictionary
    applied = []
    result = text
    for entry in entries:
        after = apply_native_dictionary(result, [entry])
        if after != result:
            applied.append({"source": entry.source, "target": entry.target, "mode": entry.replace_mode})
            result = after
    assert result == apply_native_dictionary(text, entries), "stepwise application must equal the legacy function"
    return result, applied


def postprocess(cue: dict, raw_text: str, loaded: dict) -> dict:
    """Legacy post-processing of one cue: post dictionary, in-place required terms, review flags. Nothing is appended."""
    from voice_dictionary import apply_native_dictionary, ensure_required_terms, has_unreviewed_kana, missing_required_terms
    glossary = loaded["glossary"]
    after_post, post_applied = _apply_traced(raw_text, loaded["post"])
    text = ensure_required_terms(cue["input_text"], after_post, glossary)
    required_missing = missing_required_terms(cue["input_text"], text, glossary)
    gpt_missing = [entry.target for entry in loaded["gpt"] if entry.source not in glossary and entry.source in cue["pre_text"] and entry.target not in text]
    missing_terms = list(dict.fromkeys(required_missing + gpt_missing))
    allowed = [entry.target for entry in loaded["required_glossary_entries"] + loaded["gpt"] + loaded["post"]]
    kana = has_unreviewed_kana(text, allowed)
    flags: list[str] = []
    if kana:
        flags += ["TRANSLATION_JAPANESE_PRESENT", "CHECK"]
    if missing_terms:
        flags += ["TRANSLATION_TERM_MISSING", "CHECK"]
    if "[待确认]" in text:
        flags += ["TRANSLATION_UNCERTAIN", "CHECK"]
    return {"target_text": text, "flags": sorted(set(flags)), "trace": {"raw_model_text": raw_text, "post_dictionary": post_applied, "after_post": after_post,
                                                                         "required_terms_replaced_in_place": after_post != text, "missing_terms": missing_terms,
                                                                         "japanese_letters_remaining": kana, "uncertain_marker": "[待确认]" in text}}


# --------------------------------------------------------------------------- Interface

def translation_signature(*, segments_sha256: str, source_revision_id: str, dictionary_sha256: str, accepted: list[str], prompt: str, engine_sha256: str,
                          model_sha256: str, params: dict) -> str:
    """Everything that changes the translation output (plan 8.4 / 10.3): segments, dictionary, prompt, engine, model, parameters."""
    return signature({"adapter_version": TRANSLATION_ADAPTER_VERSION, "protocol": PROTOCOL_ID, "segments_sha256": segments_sha256, "source_revision_id": source_revision_id,
                      "dictionary_sha256": dictionary_sha256, "accepted_dictionary_warnings": sorted(accepted), "prompt_sha256": sha256_text(prompt),
                      "engine_sha256": engine_sha256, "model_sha256": model_sha256,
                      "parameters": {key: params[key] for key in ("batch_size", "context_size", "gpu_layers", "temperature", "top_p", "use_native_dictionaries", "native_prompt_mode")}})


def translate(request: dict, *, runtime: TranslationRuntime) -> dict:
    """TranslationRequest -> {translations_ref, checkpoint_ref, raw_exchange_refs, issues} (plan 6.8)."""
    from subtitle_axis_align import normalize_text
    from voice_dictionary import native_glossary_prompt
    require_record("TranslationRequest", request)
    snapshot = request["config_snapshot"]
    settings = snapshot["engine_settings"].get("translation") if isinstance(snapshot["engine_settings"].get("translation"), dict) else {}
    params = effective_parameters(settings)
    segments_path = safe_join(Path(runtime.project_dir), request["segments_ref"]["path"])
    if not segments_path.is_file() or sha256_bytes(segments_path.read_bytes()) != request["segments_ref"]["sha256"]:
        raise ContractError("ARTIFACT_CHANGED", "the segments file differs from the reference in the translation request", details={"path": str(segments_path)})
    segment_set = load_record("SegmentSet", segments_path)
    if segment_set["source_revision_id"] != request["source_revision_id"]:
        raise ContractError("TRANSLATION_IDENTITY_ERROR", "segments belong to another source revision", details={"expected": request["source_revision_id"], "found": segment_set["source_revision_id"]})
    engine = runtime.resources.get("translation_engine")
    model = runtime.resources.get("translation_model")
    if engine is None or model is None:
        raise ContractError("RESOURCE_MISSING", "translation needs the translation_engine and translation_model roles; no server was started",
                            details={"missing": [role for role, value in (("translation_engine", engine), ("translation_model", model)) if value is None]})
    executable = _resource_file(engine, ("llama-server.exe", "llama-server", "server.exe"))
    model_path = _resource_file(model, ("Sakura-Galtransl-7B-v3.7.gguf",))
    if snapshot["dictionary_revision_id"] == "none":
        raise ContractError("INPUT_MISSING", "this request has no dictionary revision; submit creates one from the dictionary settings", details={"dictionary_revision_id": "none"})
    loaded = dictionary.load_for_translation(runtime.data_root, snapshot["dictionary_revision_id"], accepted_ids=snapshot["accepted_dictionary_warnings"], settings=settings)
    sig = translation_signature(segments_sha256=request["segments_ref"]["sha256"], source_revision_id=request["source_revision_id"], dictionary_sha256=loaded["content_sha256"],
                                accepted=snapshot["accepted_dictionary_warnings"], prompt=loaded["prompt"], engine_sha256=engine["resource_ref"]["content_sha256"],
                                model_sha256=model["resource_ref"]["content_sha256"], params=params)
    out = Path(runtime.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    segments = segment_set["segments"]
    parents = {parent["id"]: parent for parent in segment_set["parents"]}
    cues: list[dict] = []
    for segment in segments:
        input_text = normalize_text(segment["source_text"])          # the legacy translation input is the SRT text, i.e. the normalized form
        pre_text, pre_applied = _apply_traced(input_text, loaded["pre"])
        context = [{"parent_id": span["parent_id"], "text": parents[span["parent_id"]]["text"]} for span in segment["source_ranges"] if span["parent_id"] in parents]
        cues.append({**segment, "input_text": input_text, "pre_text": pre_text, "pre_applied": pre_applied, "context": context})
    issues: list[dict] = []
    stats = {"requests": 0, "ambiguous_splits": 0, "resumed_cues": 0}
    checkpoint = Checkpoint(Path(request["checkpoint_dir"]), sig)
    completed = checkpoint.completed(segments)
    stats["resumed_cues"] = len(completed)
    pending = [cue for cue in cues if cue["cue_id"] not in completed]
    runtime.emit({"event": "translation_started", "cues": len(cues), "resumed": len(completed), "pending": len(pending), "signature": sig})
    server_logs = out / "server"
    if pending:
        by_id = {cue["index"]: cue for cue in cues}
        with OwnedServer(executable=executable, model=model_path, params=params, log_dir=server_logs, runtime=runtime, engine_ref=engine["resource_ref"],
                         model_ref=model["resource_ref"], startup_timeout=settings.get("startup_timeout_seconds")) as server, Transport(runtime) as transport:
            batch_size = params["batch_size"]
            for offset in range(0, len(pending), batch_size):
                current = pending[offset:offset + batch_size]
                batch = [{"id": cue["index"], "text": cue["pre_text"], "cue_id": cue["cue_id"], "context": cue["context"]} for cue in current]
                batch_source_text = "\n".join(str(item["text"]) for item in batch)
                batch_reference_text = batch_source_text + "\n" + "\n".join(str(parent["text"]) for item in batch for parent in item["context"])
                required = native_glossary_prompt(loaded["required_glossary_entries"], batch_reference_text)
                native = native_glossary_prompt(loaded["gpt"], batch_reference_text, excluded_sources=set(loaded["glossary"]))
                glossary_text = "\n".join(section for section in (required, native) if section)
                runtime.emit({"event": "translation_batch", "ids": [item["id"] for item in batch], "completed": len(checkpoint.completed(segments)), "total": len(cues)})
                _translate_batch(batch, by_id=by_id, transport=transport, checkpoint=checkpoint, endpoint=server.endpoint, model_name=model_path.name,
                                 system_prompt=loaded["prompt"], params=params, glossary_text=glossary_text, runtime=runtime, stats=stats)
        completed = checkpoint.completed(segments)
    missing = [cue["cue_id"] for cue in cues if cue["cue_id"] not in completed]
    if missing:
        raise ContractError("TRANSLATION_IDENTITY_ERROR", f"{len(missing)} cue(s) have no checkpointed translation after the batches completed", details={"missing": missing})

    # exchanges and traces are copied into the attempt directory so the stage manifest is self-contained
    exchange_root = out / "exchanges"
    if (checkpoint.dir / "exchanges").is_dir():
        if exchange_root.exists():
            shutil.rmtree(exchange_root)
        shutil.copytree(checkpoint.dir / "exchanges", exchange_root)
    records: list[dict] = []
    traces: dict[str, dict] = {}
    empty_targets: list[str] = []
    for cue in cues:
        entry = completed[cue["cue_id"]]
        processed = postprocess(cue, entry["raw_text"], loaded)
        if not processed["target_text"].strip():
            empty_targets.append(cue["cue_id"])
        exchange = out / entry["exchange"]
        traces[cue["cue_id"]] = {"input_text": cue["input_text"], "pre_dictionary": cue["pre_applied"], "after_pre": cue["pre_text"], "context_parents": len(cue["context"]),
                                 "mapping": entry["mapping"], "exchange": entry["exchange"], **processed["trace"]}
        records.append({"cue_id": cue["cue_id"], "source_revision_id": request["source_revision_id"], "source_text_sha256": sha256_text(cue["source_text"]),
                        "target_text": processed["target_text"], "request_ref": artifact_ref(out, exchange / "request.json"),
                        "raw_response_ref": artifact_ref(out, exchange / "response.body"), "engine_ref": engine["resource_ref"], "model_ref": model["resource_ref"],
                        "dictionary_revision_id": snapshot["dictionary_revision_id"], "prompt_sha256": entry["prompt_sha256"], "postprocess_trace_ref": None,
                        "flags": processed["flags"]})
    if empty_targets:
        raise ContractError("TRANSLATION_IDENTITY_ERROR", "a non-empty source cue received an empty translation after post-processing; the result is not committed",
                            details={"cue_ids": empty_targets, "rule_id": "QC17_TRANSLATION"})
    atomic_write_text(out / "postprocess_trace.json", render_json({"schema_version": SCHEMA_VERSION, "signature": sig, "prompt_mode": loaded["prompt_mode"], "cues": traces}))
    trace_ref = artifact_ref(out, out / "postprocess_trace.json")
    for record in records:
        record["postprocess_trace_ref"] = trace_ref
    flag_counts: dict[str, int] = {}
    for record in records:
        for flag in record["flags"]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    for flag, code in (("TRANSLATION_TERM_MISSING", "TRANSLATION_TERM_MISSING"), ("TRANSLATION_JAPANESE_PRESENT", "TRANSLATION_JAPANESE_PRESENT"),
                       ("TRANSLATION_UNCERTAIN", "TRANSLATION_UNCERTAIN")):
        if flag_counts.get(flag):
            issues.append({"code": code, "severity": "WARNING", "message": f"{flag_counts[flag]} cue(s) flagged {flag}; the text is kept for review, nothing was appended or removed",
                           "details": {"cues": [record["cue_id"] for record in records if flag in record["flags"]]}})
    if loaded["diagnostics"]["unsupported"]:
        issues.append({"code": "DICTIONARY_UNSUPPORTED_ACCEPTED", "severity": "INFO", "message": f"{len(loaded['diagnostics']['unsupported'])} unsupported dictionary entr(y/ies) were accepted and skipped",
                       "details": {"ids": loaded["diagnostics"]["unsupported_ids"]}})
    if not segments:
        issues.append({"code": "TRANSLATION_EMPTY_SOURCE", "severity": "WARNING", "message": "the source revision has no cues; no model server was started", "details": {}})
    translation_set = require_record("TranslationSet", {
        "schema_version": SCHEMA_VERSION, "source_revision_id": request["source_revision_id"], "translation_signature": sig, "dictionary_revision_id": snapshot["dictionary_revision_id"],
        "engine_ref": engine["resource_ref"], "model_ref": model["resource_ref"], "parameters": params, "status": "EMPTY" if not records else "OK", "records": records,
        "extensions": {"adapter_version": TRANSLATION_ADAPTER_VERSION, "protocol": PROTOCOL_ID, "model_name": model_path.name, "prompt_sha256": sha256_text(loaded["prompt"]),
                       "prompt_mode": loaded["prompt_mode"], "dictionary_content_sha256": loaded["content_sha256"], "entry_counts": loaded["diagnostics"]["entry_counts"],
                       "stats": stats, "flag_counts": flag_counts, "run_id": request["run_id"], "created_at": utc_now()},
    })
    atomic_write_text(out / "translations.json", render_json(translation_set))
    summary = {"schema_version": SCHEMA_VERSION, "signature": sig, "checkpoint_dir": str(checkpoint.dir), "total": len(cues), "completed": len(completed),
               "resumed_from_earlier_attempts": stats["resumed_cues"], "requests_this_attempt": stats["requests"], "ambiguous_splits": stats["ambiguous_splits"]}
    atomic_write_text(out / "checkpoint.json", render_json(summary))
    exchange_refs = [artifact_ref(out, path) for path in sorted(p for p in exchange_root.rglob("*") if p.is_file())] if exchange_root.is_dir() else []
    return {"translations_ref": artifact_ref(out, out / "translations.json"), "checkpoint_ref": artifact_ref(out, out / "checkpoint.json"),
            "raw_exchange_refs": exchange_refs, "issues": issues}


__all__ = ["TRANSLATION_ADAPTER_VERSION", "PROTOCOL_ID", "MAX_ATTEMPTS", "CONNECT_TIMEOUT_SECONDS", "STALL_NOTICE_SECONDS", "TranslationRuntime", "effective_parameters",
           "server_argv", "launch_spec", "OwnedServer", "Transport", "Checkpoint", "postprocess", "translation_signature", "translate"]
