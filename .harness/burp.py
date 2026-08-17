from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

DEFAULT_GLOBAL_ROOT = Path(os.environ.get("AWOKI_GLOBAL_ROOT", "~/.awoki")).expanduser()
DEFAULT_BURP_URL = os.environ.get("AWOKI_BURP_URL", "http://host.docker.internal:9876")
DEFAULT_TARGET = os.environ.get("AWOKI_BURP_TARGET", "local")
DEFAULT_TIMEOUT = int(os.environ.get("AWOKI_BURP_TIMEOUT", "60"))
DEFAULT_CALL_TIMEOUT = int(os.environ.get("AWOKI_BURP_CALL_TIMEOUT", "180"))

REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?im)^(Authorization:\s*Bearer\s+).*$"), r"\1<BEARER_REDACTED>"),
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)[^\\\"\r\n]+"), r"\1<BEARER_REDACTED>"),
    (re.compile(r"(?im)^(Authorization:\s*Basic\s+).*$"), r"\1<BASIC_REDACTED>"),
    (re.compile(r"(?i)(Authorization:\s*Basic\s+)[^\\\"\r\n]+"), r"\1<BASIC_REDACTED>"),
    (re.compile(r"(?im)^(Authorization:\s*).*$"), r"\1<AUTH_REDACTED>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"), "<JWT_REDACTED>"),
    (re.compile(r"(?i)(api[-_]?key|access[-_]?token|token|secret|password|passwd|pwd)=([^&;\s]+)"), r"\1=<VALUE_REDACTED>"),
]
SENSITIVE_COOKIE_RE = re.compile(r"(?i)(session|sid|token|auth|jwt|clearance|csrf|xsrf|appsession)")
STATIC_EXTS = (".png", ".gif", ".jpg", ".jpeg", ".webp", ".ico", ".svg", ".css", ".js", ".map", ".woff", ".woff2", ".ttf")
THIRD_PARTY_HINTS = [
    "google-analytics.com", "googletagmanager.com", "googlesyndication.com", "googleadservices.com",
    "doubleclick.net", "consentmanager.net", "hotjar.com", "cloudflareinsights.com", "sentry.io",
]

TOOL_CANDIDATES = {
    "history": ["get_proxy_http_history", "GetProxyHttpHistory", "proxy_http_history", "proxy_history"],
    "history_regex": ["get_proxy_http_history_regex", "GetProxyHttpHistoryRegex", "proxy_http_history_regex"],
    "websocket_history": ["get_proxy_websocket_history", "GetProxyWebsocketHistory", "proxy_websocket_history"],
    "organizer": ["get_organizer_items", "GetOrganizerItems", "organizer_items"],
    "organizer_regex": ["get_organizer_items_regex", "GetOrganizerItemsRegex", "organizer_items_regex"],
    "active_editor": ["get_active_editor_contents", "GetActiveEditorContents", "active_editor_contents"],
    "set_active_editor": ["set_active_editor_contents", "SetActiveEditorContents", "set_active_editor"],
    "send_http1": ["send_http1_request", "SendHttp1Request", "send_http_1_request", "http1_request"],
    "send_http2": ["send_http2_request", "SendHttp2Request", "send_http_2_request", "http2_request"],
    "create_repeater": ["create_repeater_tab", "CreateRepeaterTab", "send_to_repeater", "repeater_tab"],
    "create_repeater_http2": ["create_repeater_tab_http2", "CreateRepeaterTabHttp2", "send_to_repeater_http2"],
    "send_to_intruder": ["send_to_intruder", "SendToIntruder", "intruder_send"],
}

ACTIVE_ACTIONS = {
    "send_http1",
    "send_http2",
    "create_repeater",
    "create_repeater_http2",
    "send_to_intruder",
    "set_active_editor",
    "active_editor",
}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def project_slug(project_related: str = "") -> str:
    value = clean(project_related) if str(project_related or "").strip() else "global"
    return value or "global"


def is_global_project(project_related: str = "") -> bool:
    return project_slug(project_related) == "global"


def run_id(prefix: str = "burp", project_related: str = "") -> str:
    # Project first so runs sort visually by project/case:
    #   asd__2026-06-25_230447__history_regex
    #   global__2026-06-25_230447__history_regex
    return f"{project_slug(project_related)}__{dt.datetime.now().strftime('%Y-%m-%d_%H%M%S')}__{clean(prefix)}"


def clean(value: str) -> str:
    value = str(value or "").strip().replace(" ", "-")
    value = re.sub(r"[^A-Za-z0-9._:-]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-._:")
    return value or "unspecified"


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]


def global_root() -> Path:
    return Path(os.environ.get("AWOKI_GLOBAL_ROOT", str(DEFAULT_GLOBAL_ROOT))).expanduser().resolve()


def burp_root() -> Path:
    return global_root() / "state" / "burp"


def runs_dir() -> Path:
    return burp_root() / "runs"


def profile_path() -> Path:
    return burp_root() / "profile.json"


def latest_path() -> Path:
    return burp_root() / "latest-run-path.txt"


def awoki_root() -> Path:
    return Path(os.environ.get("AWOKI_ROOT") or os.environ.get("HARNESS_ROOT", ".")).expanduser().resolve()


def project_burp_dir(project_related: str) -> Path | None:
    pid = project_slug(project_related)
    if pid == "global":
        return None
    project_dir = awoki_root() / "workspace" / "projects" / pid
    return project_dir / "artifacts" / "burp"


def project_burp_paths(project_related: str) -> dict[str, Path] | None:
    base = project_burp_dir(project_related)
    if base is None:
        return None
    return {
        "base": base,
        "runs": base / "runs.jsonl",
        "latest": base / "latest.md",
        "handoff": base / "handoff.md",
        "extracted": base / "extracted",
        "observations": base / "observations.jsonl",
        "host_summaries": base / "host-summaries.jsonl",
        "host_reports": base / "host-reports",
        "tasks": base / "tasks",
        "latest_task": base / "latest-task.txt",
    }


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(mode)
    except Exception:
        pass


def write_text(path: Path, text: str, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", errors="replace")
    if mode is not None:
        try:
            tmp.chmod(mode)
        except Exception:
            pass
    tmp.replace(path)
    if mode is not None:
        try:
            path.chmod(mode)
        except Exception:
            pass


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_json(path: Path, obj: Any, mode: int | None = None) -> None:
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=mode)


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush(); os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    tmp.replace(path)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush(); os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(read_text(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                obj.setdefault("_source_file", str(path))
                obj.setdefault("_line", i)
                rows.append(obj)
        except Exception:
            rows.append({"kind": "burp_parse_error", "text": f"Invalid JSONL {path}:{i}", "_source_file": str(path), "_line": i})
    return rows


def redact(text: str) -> str:
    out = text
    for pat, repl in REDACTION_PATTERNS:
        out = pat.sub(repl, out)
    def repl_cookie(m: re.Match[str]) -> str:
        values = []
        for part in m.group(1).split(";"):
            if "=" in part:
                values.append(part.split("=", 1)[0].strip() + "=<COOKIE_REDACTED>")
        return "Cookie: " + "; ".join(values)
    out = re.sub(r"(?im)^Cookie:\s*(.*)$", repl_cookie, out)
    out = re.sub(r"(?im)^Set-Cookie:\s*([^=;\s]+)=.*$", r"Set-Cookie: \1=<COOKIE_REDACTED>", out)
    out = re.sub(r"(?i)(Set-Cookie:\s*[^=;\s]+=)[^\\;\"\r\n]+", r"\1<COOKIE_REDACTED>", out)
    out = re.sub(r"(?i)(Cookie:\s*[^\\\"\r\n]+)", lambda m: re.sub(r"=([^;\\\"\r\n]+)", r"=<COOKIE_REDACTED>", m.group(1)), out)
    return out


def default_profile() -> dict[str, Any]:
    return {
        "version": 1,
        "default_target": DEFAULT_TARGET,
        "targets": {
            "local": {
                "kind": "burp_mcp_sse",
                "base_url": DEFAULT_BURP_URL,
                "host_header": "127.0.0.1:9876",
                "origin": "http://127.0.0.1:9876",
                "notes": "Docker-to-host default for PortSwigger Burp MCP server.",
            }
        },
        "sources": {
            "history": {"tool_alias": "history", "arguments": {"count": "{{count}}", "offset": "{{offset}}"}},
            "history_regex": {"tool_alias": "history_regex", "arguments": {"regex": "{{regex}}", "count": "{{count}}", "offset": "{{offset}}"}},
            "websocket_history": {"tool_alias": "websocket_history", "arguments": {"count": "{{count}}", "offset": "{{offset}}"}},
            "organizer": {"tool_alias": "organizer", "arguments": {"count": "{{count}}", "offset": "{{offset}}"}},
            "active_editor": {"tool_alias": "active_editor", "arguments": {}},
            "repeater": {"tool_alias": "active_editor", "arguments": {}, "fallback_note": "Focus the Repeater tab in Burp, then pull active editor."},
            "intruder": {"tool_alias": "active_editor", "arguments": {}, "fallback_note": "Focus the Intruder request editor in Burp, then pull active editor."},
        },
        "actions": {
            "send_http1": {"tool_alias": "send_http1"},
            "send_http2": {"tool_alias": "send_http2"},
            "create_repeater": {"tool_alias": "create_repeater"},
            "create_repeater_http2": {"tool_alias": "create_repeater_http2"},
            "send_to_intruder": {"tool_alias": "send_to_intruder"},
            "get_active_editor": {"tool_alias": "active_editor"},
            "set_active_editor": {"tool_alias": "set_active_editor"}
        },
        "rag_policy": {
            "index": ["requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"],
            "do_not_index": ["raw/*.mcp.json", "redacted/*.txt", "extracted/*.http", "*/extracted/*.http"],
            "raw_evidence": "kept_exact_on_disk_not_loaded_broadly",
        },
    }


def ensure_store() -> dict[str, Any]:
    for p in [burp_root(), runs_dir()]:
        ensure_dir(p)
    if not profile_path().exists():
        write_json(profile_path(), default_profile(), mode=0o600)
    return status()


def load_profile() -> dict[str, Any]:
    ensure_store()
    return read_json(profile_path())


def status() -> dict[str, Any]:
    ensure_dir(burp_root())
    ensure_dir(runs_dir())
    runs = sorted([p for p in runs_dir().iterdir() if p.is_dir()]) if runs_dir().exists() else []
    latest = read_text(latest_path()).strip() if latest_path().exists() else ""
    return {
        "status": "ok",
        "burp_root": str(burp_root()),
        "profile": str(profile_path()),
        "default_url": DEFAULT_BURP_URL,
        "runs": len(runs),
        "latest_run": latest,
        "rag_policy": "index compact redacted summaries only; raw evidence is not indexed",
    }


def latest_run_dir() -> Path:
    if not latest_path().exists():
        raise SystemExit("No latest Burp run. Use `awoki_burp.py start` or a pull command first.")
    value = read_text(latest_path()).strip()
    if not value:
        raise SystemExit("latest-run-path.txt is empty")
    return Path(value)


def create_run(source_type: str, target: str = "local", project_related: str = "", tags: list[str] | None = None, note: str = "") -> Path:
    ensure_store()
    project_for_name = project_slug(project_related)
    manifest_project = "" if project_for_name == "global" else project_for_name
    rid_base = run_id(source_type, project_related=project_for_name)
    rid = rid_base
    rd = runs_dir() / rid
    suffix = 1
    while rd.exists():
        suffix += 1
        rid = f"{rid_base}-{suffix}"
        rd = runs_dir() / rid
    for d in [rd, rd / "raw", rd / "redacted", rd / "debug"]:
        ensure_dir(d)
    manifest = {
        "schema": "awoki-burp-run-v1",
        "run_id": rid,
        "created_at": now(),
        "updated_at": now(),
        "target": target,
        "source_type": source_type,
        "project_related": manifest_project,
        "project_prefix": project_for_name,
        "tags": tags or [],
        "status": "created",
        "note": note,
    }
    write_json(rd / "run-manifest.json", manifest)
    for name, content in {
        "requests.jsonl": "",
        "endpoints.md": "# Endpoints\n\n",
        "auth-cookies.md": "# Auth and cookies\n\n",
        "variables.md": "# Variables and parameters\n\n",
        "interesting.md": "# Interesting observations\n\n",
        "handoff.md": "# Handoff\n\nUse compact files first. Do not read raw/ broadly.\n",
    }.items():
        write_text(rd / name, content)
    write_text(latest_path(), str(rd) + "\n")
    write_text(burp_root() / "latest.md", f"# Latest Burp run\n\n`{rd}`\n")
    return rd


class McpSseClient:
    def __init__(self, url: str, host_header: str | None = None, origin: str | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.url = url.rstrip("/")
        self.u = urllib.parse.urlparse(self.url)
        if self.u.scheme != "http" or not self.u.hostname:
            raise ValueError("Only http:// MCP SSE URLs are supported by Awoki's dependency-free client")
        self.host_header = host_header or self.u.netloc
        self.origin = origin or f"http://{self.u.netloc}"
        self.timeout = timeout
        self.events: queue.Queue[dict[str, str]] = queue.Queue()
        self.post_path: str | None = None
        self.rid = 0
        self.resp: http.client.HTTPResponse | None = None

    def _conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.u.hostname, self.u.port or 80, timeout=self.timeout)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Host": self.host_header,
            "Origin": self.origin,
            "Accept": "application/json, text/event-stream, */*",
            "User-Agent": "awoki-burp/1.0",
        }
        if extra:
            h.update(extra)
        return h

    def connect(self) -> None:
        path = self.u.path or "/"
        if self.u.query:
            path += "?" + self.u.query
        c = self._conn()
        c.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        for k, v in self._headers().items():
            c.putheader(k, v)
        c.endheaders()
        r = c.getresponse()
        if r.status >= 400:
            raise RuntimeError(f"SSE connect HTTP {r.status}: {r.read(1000)!r}")
        self.resp = r
        threading.Thread(target=self._reader, daemon=True).start()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=1)
            except queue.Empty:
                continue
            if ev.get("event") == "endpoint":
                endpoint = ev.get("data", "").strip()
                pu = urllib.parse.urlparse(endpoint)
                self.post_path = pu.path + ("?" + pu.query if pu.query else "") if pu.scheme and pu.netloc else endpoint
                return
        raise TimeoutError("No MCP endpoint event received. Try URL with or without /sse and confirm Burp MCP is enabled.")

    def _reader(self) -> None:
        assert self.resp is not None
        event = "message"
        data: list[str] = []
        try:
            while True:
                raw = self.resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if data:
                        self.events.put({"event": event, "data": "\n".join(data)})
                    event = "message"; data = []
                elif line.startswith(":"):
                    continue
                elif line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data.append(line[len("data:"):].lstrip())
        except Exception as exc:
            self.events.put({"event": "error", "data": repr(exc)})

    def post(self, payload: dict[str, Any]) -> None:
        if not self.post_path:
            raise RuntimeError("No MCP post endpoint discovered")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        c = self._conn()
        c.putrequest("POST", self.post_path, skip_host=True, skip_accept_encoding=True)
        for k, v in self._headers({"Content-Type": "application/json", "Content-Length": str(len(body))}).items():
            c.putheader(k, v)
        c.endheaders(body)
        r = c.getresponse()
        if r.status >= 400:
            raise RuntimeError(f"MCP POST HTTP {r.status}: {r.read(1000)!r}")
        r.read()

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
        self.rid += 1
        rid = self.rid
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.post(payload)
        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=1)
            except queue.Empty:
                continue
            if ev.get("event") == "error":
                raise RuntimeError(ev.get("data"))
            if ev.get("event") != "message":
                continue
            try:
                msg = json.loads(ev.get("data", ""))
            except Exception:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg
        raise TimeoutError(f"Timeout waiting for {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.post(payload)

    def init(self) -> None:
        self.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "awoki-burp", "version": "1.0"}})
        self.notify("notifications/initialized")

    def tools(self) -> dict[str, Any]:
        return self.request("tools/list", {})

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: int = DEFAULT_CALL_TIMEOUT) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)


def client_for_target(target: str) -> McpSseClient:
    profile = load_profile()
    t = profile.get("targets", {}).get(target)
    if not t:
        raise SystemExit(f"Unknown Burp target: {target}. Edit {profile_path()}.")
    base = str(t.get("base_url") or DEFAULT_BURP_URL)
    attempts = [base]
    if not base.rstrip("/").endswith("/sse"):
        attempts.append(base.rstrip("/") + "/sse")
    last: Exception | None = None
    for url in attempts:
        try:
            c = McpSseClient(url, host_header=t.get("host_header"), origin=t.get("origin"), timeout=DEFAULT_TIMEOUT)
            c.connect(); c.init()
            return c
        except Exception as exc:
            last = exc
    raise RuntimeError(f"Could not connect to Burp MCP target {target}: {last}")


def tool_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    tools = result.get("result", {}).get("tools", [])
    return tools if isinstance(tools, list) else []


def discover_tool_name(tools: list[dict[str, Any]], alias: str) -> str | None:
    names = {str(t.get("name", "")): t for t in tools}
    lowered = {n.lower(): n for n in names}
    for cand in TOOL_CANDIDATES.get(alias, [alias]):
        if cand in names:
            return cand
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    alias_words = alias.replace("_", " ").lower().split()
    for n in names:
        low = n.replace("_", " ").lower()
        if all(w in low for w in alias_words):
            return n
    return None


def render_template(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        exact = re.fullmatch(r"\{\{([A-Za-z0-9_]+)\}\}", value.strip())
        if exact and exact.group(1) in ctx:
            return ctx[exact.group(1)]
        out = value
        for k, v in ctx.items():
            out = out.replace("{{" + k + "}}", str(v))
        return out
    if isinstance(value, list):
        return [render_template(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: render_template(v, ctx) for k, v in value.items()}
    return value


def extract_texts(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        if obj.strip():
            out.append(obj)
    elif isinstance(obj, list):
        for x in obj:
            out.extend(extract_texts(x))
    elif isinstance(obj, dict):
        if isinstance(obj.get("text"), str):
            out.append(obj["text"])
        for v in obj.values():
            out.extend(extract_texts(v))
    return out



def json_walk_values(obj: Any) -> Iterable[Any]:
    """Yield obj and all nested JSON-like values."""
    yield obj
    if isinstance(obj, dict):
        for v in obj.values():
            yield from json_walk_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from json_walk_values(v)


def maybe_json_loads(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def decode_json_string_fragment(fragment: str) -> str:
    """Decode a JSON string fragment even when the enclosing object was truncated.

    PortSwigger's Burp MCP history tools serialize each history item to JSON and then
    truncate the serialized string at 5000 chars. If truncation happens inside the
    request/response value, the outer JSON becomes invalid. This function decodes
    common JSON escapes from a captured value fragment without requiring the whole
    object to be valid JSON.
    """
    value = fragment
    # Try strict JSON-string decoding first.
    try:
        return json.loads('"' + value + '"')
    except Exception:
        pass
    # Tolerant fallback: protect unknown broken trailing escapes, then decode basics.
    value = value.replace('\\r\\n', '\r\n').replace('\\n', '\n').replace('\\r', '\r')
    value = value.replace('\\t', '\t').replace('\\"', '"').replace('\\/', '/')
    value = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value)
    return value


def extract_json_string_value(text: str, key: str) -> tuple[str, bool] | None:
    """Return (decoded_value, closed_quote_seen) for a JSON string key.

    Works for valid and truncated fragments such as: "request":"GET /...<EOF>.
    """
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', text)
    if not m:
        return None
    i = m.end()
    out: list[str] = []
    escaped = False
    closed = False
    while i < len(text):
        ch = text[i]
        if escaped:
            out.append('\\' + ch)
            escaped = False
        elif ch == '\\':
            escaped = True
        elif ch == '"':
            closed = True
            break
        else:
            out.append(ch)
        i += 1
    if escaped:
        out.append('\\')
    return decode_json_string_fragment(''.join(out)), closed


def extract_json_scalar_value(text: str, key: str) -> Any:
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*([^,}\]]+)', text)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except Exception:
        return raw.strip('"')


def tolerant_burp_object_from_text(text: str) -> dict[str, Any] | None:
    """Extract request/response-ish fields from valid or truncated Burp MCP text."""
    req_pair = extract_json_string_value(text, "request") or extract_json_string_value(text, "requestRaw") or extract_json_string_value(text, "httpRequest")
    resp_pair = extract_json_string_value(text, "response") or extract_json_string_value(text, "responseRaw") or extract_json_string_value(text, "httpResponse")
    if not req_pair and not resp_pair:
        return None
    obj: dict[str, Any] = {}
    if req_pair:
        obj["request"] = req_pair[0]
        obj["request_truncated"] = not req_pair[1]
    if resp_pair:
        obj["response"] = resp_pair[0]
        obj["response_truncated"] = not resp_pair[1]
    for key in ["id", "burp_id", "history_id", "message_id", "tool_id", "statusCode", "status_code"]:
        val = extract_json_scalar_value(text, key)
        if val is not None:
            obj[key] = val
    return obj


def flatten_json_candidates(obj: Any) -> list[Any]:
    """Return dict/list candidates from nested MCP result content and JSON-in-text."""
    out: list[Any] = []
    seen_ids: set[int] = set()

    def add(x: Any) -> None:
        if isinstance(x, (dict, list)):
            ident = id(x)
            if ident in seen_ids:
                return
            seen_ids.add(ident)
            out.append(x)

    def consume(x: Any) -> None:
        add(x)
        if isinstance(x, dict):
            for v in x.values():
                consume(v)
        elif isinstance(x, list):
            for v in x:
                consume(v)
        elif isinstance(x, str):
            text = x.strip()
            if not text:
                return
            parsed = maybe_json_loads(text)
            if parsed is not None:
                consume(parsed)
            # Line-delimited JSON objects are how paginated Burp MCP history is often rendered.
            for line in text.splitlines():
                parsed_line = maybe_json_loads(line.strip())
                if parsed_line is not None:
                    consume(parsed_line)
            # Raw decoder catches adjacent JSON values and JSON arrays inside banners.
            dec = json.JSONDecoder()
            i = 0
            while i < len(text):
                j = min([k for k in [text.find('{', i), text.find('[', i)] if k >= 0], default=-1)
                if j < 0:
                    break
                try:
                    parsed_obj, end = dec.raw_decode(text[j:])
                    consume(parsed_obj)
                    i = j + max(1, end)
                except Exception:
                    i = j + 1
            tolerant = tolerant_burp_object_from_text(text)
            if tolerant:
                add(tolerant)

    consume(obj)
    return out


def extract_json_objects(text: str) -> list[dict[str, Any]]:
    """Backward-compatible helper: dict candidates from text, including truncated Burp items."""
    return [x for x in flatten_json_candidates(text) if isinstance(x, dict)]

def split_head_body(msg: str) -> tuple[str, str]:
    if "\r\n\r\n" in msg:
        return msg.split("\r\n\r\n", 1)
    if "\n\n" in msg:
        return msg.split("\n\n", 1)
    return msg, ""


def parse_headers(lines: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    headers: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for line in lines:
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        key = k.strip().lower()
        value = v.strip()
        headers[key] = value
        multi.setdefault(key, []).append(value)
    return headers, multi


def cookie_names(value: str) -> list[str]:
    names: list[str] = []
    for part in (value or "").split(";"):
        if "=" in part:
            name = part.split("=", 1)[0].strip()
            if name and name not in names:
                names.append(name)
    return names


def set_cookie_names(values: list[str]) -> list[str]:
    names: list[str] = []
    for v in values:
        name = str(v).split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return names


def qnames(target: str) -> tuple[str, list[str]]:
    u = urllib.parse.urlsplit(target)
    names: list[str] = []
    for key, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True):
        if key and key not in names:
            names.append(key)
    return u.path or target, names


def json_keys(body: str) -> list[str]:
    b = body.strip()
    if not b.startswith("{"):
        return []
    try:
        o = json.loads(b)
        return list(o.keys())[:80] if isinstance(o, dict) else []
    except Exception:
        return re.findall(r'"([^"]+)"\s*:', b)[:80]


def form_names(body: str) -> list[str]:
    if "=" not in body:
        return []
    out: list[str] = []
    for part in body.split("&"):
        if "=" in part:
            key = urllib.parse.unquote_plus(part.split("=", 1)[0]).strip()
            if key and len(key) < 100 and key not in out:
                out.append(key)
    return out[:80]


def path_ids(path: str) -> list[str]:
    return [seg for seg in path.split("/") if re.fullmatch(r"\d+|[0-9a-fA-F-]{12,}", seg or "")][:40]


def status_code(resp: str) -> int | None:
    first = (resp.splitlines() or [""])[0]
    m = re.search(r"\b(\d{3})\b", first)
    return int(m.group(1)) if m else None


def classify(host: str, path: str, req: str, resp: str) -> tuple[str, list[str]]:
    low = f"{host} {path} {req[:2000]} {resp[:2000]}".lower()
    notes: list[str] = []
    category = "first_party_or_unknown"
    if any(h in low for h in THIRD_PARTY_HINTS):
        category = "third_party_noise"; notes.append("third-party telemetry/analytics/consent/fingerprint")
    if "cloudflare" in low or "/cdn-cgi/" in low or "cf_clearance" in low or "cf_appsession" in low:
        category = "cloudflare_or_access_flow"; notes.append("cloudflare/access/challenge flow")
    if path.lower().endswith(STATIC_EXTS):
        notes.append("static asset")
    if re.search(r"(?im)^Location:\s*", resp):
        notes.append("redirect")
    if "access-control-allow-origin" in low:
        notes.append("cors header observed")
    if path_ids(path):
        notes.append("id-like path segment")
    if re.search(r"(?im)^Authorization:\s*", req):
        notes.append("authorization header present")
    if re.search(r"(?im)^Cookie:\s*", req):
        notes.append("cookies present")
    return category, sorted(set(notes))



def normalize_http_text_for_parse(text: str) -> str:
    """Normalize actual and literal line endings in HTTP-ish text."""
    out = str(text or "")
    # If the text came from a JSON string dumped into another JSON string, it may
    # still contain literal backslash-r/backslash-n sequences.
    out = out.replace('\\r\\n', '\r\n').replace('\\n', '\n').replace('\\r', '\r')
    # Preserve bodies as best as possible; for header parsing splitlines handles both.
    return out


def headers_to_lines(headers: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(v, list):
                for item in v:
                    lines.append(f"{k}: {item}")
            else:
                lines.append(f"{k}: {v}")
    elif isinstance(headers, list):
        for h in headers:
            if isinstance(h, str):
                lines.append(h)
            elif isinstance(h, dict):
                name = h.get("name") or h.get("key") or h.get("headerName") or h.get("header")
                value = h.get("value") or h.get("headerValue") or ""
                if name:
                    lines.append(f"{name}: {value}")
    return lines


def raw_http_from_request_dict(req: dict[str, Any]) -> str:
    method = str(req.get("method") or req.get("verb") or "GET").upper()
    url = str(req.get("url") or req.get("path") or req.get("target") or req.get("requestTarget") or "/")
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urllib.parse.urlsplit(url)
        target = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        host = parsed.netloc
    else:
        target = url or "/"
        host = str(req.get("host") or req.get("authority") or req.get("hostname") or "")
    version = str(req.get("httpVersion") or req.get("version") or "HTTP/1.1")
    if not version.upper().startswith("HTTP/"):
        version = "HTTP/" + version
    headers = headers_to_lines(req.get("headers") or req.get("headerList") or req.get("messageHeaders") or {})
    if host and not any(line.lower().startswith("host:") for line in headers):
        headers.insert(0, f"Host: {host}")
    body = req.get("body") or req.get("messageBody") or req.get("content") or ""
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    return "\r\n".join([f"{method} {target} {version}"] + headers) + "\r\n\r\n" + str(body or "")


def raw_http_from_response_dict(resp: dict[str, Any]) -> str:
    code = resp.get("statusCode") or resp.get("status_code") or resp.get("status") or ""
    reason = resp.get("reasonPhrase") or resp.get("reason") or resp.get("message") or ""
    version = str(resp.get("httpVersion") or resp.get("version") or "HTTP/1.1")
    if not version.upper().startswith("HTTP/"):
        version = "HTTP/" + version
    status_line = f"{version} {code} {reason}".strip()
    headers = headers_to_lines(resp.get("headers") or resp.get("headerList") or resp.get("messageHeaders") or {})
    body = resp.get("body") or resp.get("messageBody") or resp.get("content") or ""
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    return "\r\n".join([status_line] + headers) + "\r\n\r\n" + str(body or "")


def get_first_present(obj: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            return obj.get(key)
    return ""


def normalize_request_response(obj: Any) -> tuple[str, str]:
    if not isinstance(obj, dict):
        return "", ""
    req = get_first_present(obj, [
        "request", "requestRaw", "rawRequest", "httpRequest", "request_text", "requestText", "messageRequest",
    ])
    resp = get_first_present(obj, [
        "response", "responseRaw", "rawResponse", "httpResponse", "response_text", "responseText", "messageResponse",
    ])
    # Some serializers use nested message/requestResponse wrappers.
    for wrapper_key in ["requestResponse", "httpRequestResponse", "item", "message"]:
        wrapper = obj.get(wrapper_key)
        if isinstance(wrapper, dict):
            w_req, w_resp = normalize_request_response(wrapper)
            req = req or w_req
            resp = resp or w_resp
    if isinstance(req, dict):
        req = raw_http_from_request_dict(req)
    elif isinstance(req, list):
        req = "\r\n".join(map(str, req))
    if isinstance(resp, dict):
        resp = raw_http_from_response_dict(resp)
    elif isinstance(resp, list):
        resp = "\r\n".join(map(str, resp))
    return normalize_http_text_for_parse(str(req or "")), normalize_http_text_for_parse(str(resp or ""))

def parse_http_pair(req: str, resp: str, run_id_value: str, source_type: str, evidence_file: str, burp_id: Any = None, idx: int = 0, target: str = "", project_related: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    req = normalize_http_text_for_parse(req)
    resp = normalize_http_text_for_parse(resp)
    req_head, req_body = split_head_body(req)
    resp_head, _ = split_head_body(resp)
    req_lines = req_head.splitlines()
    resp_lines = resp_head.splitlines()
    first = req_lines[0] if req_lines else ""
    m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+(\S+)\s+HTTP/[\d.]+", first)
    method = m.group(1) if m else ""
    raw_target = m.group(2) if m else ""
    req_headers, _ = parse_headers(req_lines[1:])
    resp_headers, resp_multi = parse_headers(resp_lines[1:])
    host = req_headers.get("host", "")
    path, query = qnames(raw_target)
    auth = req_headers.get("authorization", "")
    cookies = cookie_names(req_headers.get("cookie", ""))
    set_cookies = set_cookie_names(resp_multi.get("set-cookie", []))
    category, notes = classify(host, path, req, resp)
    sensitive_cookies = sorted({c for c in cookies + set_cookies if SENSITIVE_COOKIE_RE.search(c)})
    parse_status = "ok" if method and path else "partial"
    return {
        "schema": "awoki-burp-request-v1",
        "run_id": run_id_value,
        "source_type": source_type,
        "target": target,
        "project_related": project_related,
        "tags": tags or [],
        "burp_id": burp_id,
        "source_object_index": idx,
        "method": method,
        "host": host,
        "path": path,
        "status_code": status_code(resp_head),
        "content_type": resp_headers.get("content-type") or req_headers.get("content-type") or "",
        "auth_header_type": auth.split()[0] if auth else "",
        "cookie_names": cookies,
        "sensitive_cookie_name_hints": sensitive_cookies,
        "set_cookie_names": set_cookies,
        "query_param_names": query,
        "form_param_names": form_names(req_body),
        "json_top_level_keys": json_keys(req_body),
        "path_variables_or_ids": path_ids(path),
        "csrf_indicators": sorted(set(re.findall(r"(?i)\b(?:csrf|xsrf)[-_a-z0-9]*\b", req))),
        "cors_indicators": ["access-control-allow-origin"] if "access-control-allow-origin" in (req + resp).lower() else [],
        "redirect_indicators": ["Location"] if re.search(r"(?im)^Location:\s*", resp) else [],
        "category": category,
        "auth_or_session_relevance": "auth/session material present" if auth or cookies else "",
        "idor_or_access_control_relevance": "id-like path segment" if path_ids(path) else "",
        "interesting_notes": notes,
        "evidence_file_if_saved": evidence_file,
        "parse_status": parse_status,
        "body_sha256_observed": sha12(req_body) if req_body else "",
        "created_at": now(),
    }


def parse_text_http_messages(text: str, run_id_value: str, source_type: str, evidence_file: str, target: str = "", project_related: str = "", tags: list[str] | None = None) -> list[dict[str, Any]]:
    starts = list(re.finditer(r"(?m)^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT)\s+\S+\s+HTTP/[\d.]+", text))
    rows: list[dict[str, Any]] = []
    for idx, match in enumerate(starts):
        chunk = text[match.start(): starts[idx + 1].start() if idx + 1 < len(starts) else len(text)]
        rows.append(parse_http_pair(chunk, "", run_id_value, source_type, evidence_file, burp_id=None, idx=idx, target=target, project_related=project_related, tags=tags))
    return rows



def rows_from_result(result: dict[str, Any], run_id_value: str, source_type: str, evidence_file: str, target: str = "", project_related: str = "", tags: list[str] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    candidates = flatten_json_candidates(result)
    seen: set[tuple[str, str, str]] = set()
    idx = 0
    for obj in candidates:
        if not isinstance(obj, dict):
            continue
        req, resp = normalize_request_response(obj)
        if not req:
            continue
        key = (req[:500], resp[:200], str(obj.get("id") or obj.get("burp_id") or obj.get("history_id") or obj.get("message_id") or idx))
        if key in seen:
            continue
        seen.add(key)
        bid = obj.get("id") or obj.get("burp_id") or obj.get("history_id") or obj.get("tool_id") or obj.get("message_id")
        row = parse_http_pair(req, resp, run_id_value, source_type, evidence_file, burp_id=bid, idx=idx, target=target, project_related=project_related, tags=tags)
        if obj.get("request_truncated"):
            row["request_truncated_by_burp_mcp"] = True
            row.setdefault("interesting_notes", []).append("request text was truncated by Burp MCP output")
        if obj.get("response_truncated"):
            row["response_truncated_by_burp_mcp"] = True
            row.setdefault("interesting_notes", []).append("response text was truncated by Burp MCP output")
        rows.append(row)
        idx += 1
    if not rows:
        joined = "\n\n".join(extract_texts(result))
        rows.extend(parse_text_http_messages(joined, run_id_value, source_type, evidence_file, target=target, project_related=project_related, tags=tags))
    if not rows:
        rows.append({
            "schema": "awoki-burp-request-v1",
            "run_id": run_id_value,
            "source_type": source_type,
            "target": target,
            "project_related": project_related,
            "tags": tags or [],
            "burp_id": None,
            "source_object_index": 0,
            "method": "",
            "host": "",
            "path": "",
            "status_code": None,
            "content_type": "",
            "auth_header_type": "",
            "cookie_names": [],
            "sensitive_cookie_name_hints": [],
            "set_cookie_names": [],
            "query_param_names": [],
            "form_param_names": [],
            "json_top_level_keys": [],
            "path_variables_or_ids": [],
            "csrf_indicators": [],
            "cors_indicators": [],
            "redirect_indicators": [],
            "category": "unknown",
            "auth_or_session_relevance": "",
            "idor_or_access_control_relevance": "",
            "interesting_notes": ["raw saved but parser could not extract HTTP metadata"],
            "evidence_file_if_saved": evidence_file,
            "parse_status": "raw_saved_unparsed",
            "created_at": now(),
        })
    return rows



def _run_pointer_row(run_dir: Path, rows_total: int | None = None, status: str = "") -> dict[str, Any]:
    manifest = load_run_manifest(run_dir)
    return {
        "schema": "awoki-project-burp-run-pointer-v1",
        "run_id": run_dir.name,
        "created_at": manifest.get("created_at", ""),
        "updated_at": manifest.get("updated_at", now()),
        "source_type": manifest.get("source_type", ""),
        "target": manifest.get("target", ""),
        "project_related": manifest.get("project_related", ""),
        "project_prefix": manifest.get("project_prefix") or project_slug(str(manifest.get("project_related", ""))),
        "status": status or manifest.get("status", ""),
        "rows_total": rows_total if rows_total is not None else manifest.get("rows_total", 0),
        "global_run_path": str(run_dir),
        "compact_files": manifest.get("compact_files", ["requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"]),
        "raw_policy": "raw evidence remains global; do not load raw/ broadly",
        "next_action": manifest.get("next_action", ""),
        "tags": manifest.get("tags", []),
        "note": manifest.get("note", ""),
    }


def update_project_burp_pointer(run_dir: Path, rows_total: int | None = None, status: str = "") -> None:
    manifest = load_run_manifest(run_dir)
    project_related = str(manifest.get("project_related", "") or "")
    paths = project_burp_paths(project_related)
    if paths is None:
        return
    for d in [paths["base"], paths["extracted"]]:
        ensure_dir(d)
    row = _run_pointer_row(run_dir, rows_total=rows_total, status=status)
    existing = read_jsonl(paths["runs"])
    existing = [r for r in existing if r.get("run_id") != row["run_id"]]
    existing.append(row)
    existing.sort(key=lambda r: str(r.get("created_at") or r.get("run_id", "")))
    write_jsonl(paths["runs"], existing)
    latest = existing[::-1]
    latest_lines = [f"# Burp runs: {project_related}", "", "This file is a compact pointer list. The model/tool may choose a bounded subset when needed.", ""]
    for item in latest:
        latest_lines.append(f"- `{item.get('run_id')}` — {item.get('source_type')} — status={item.get('status')} rows={item.get('rows_total')} path=`{item.get('global_run_path')}`")
    latest_lines.append("")
    latest_lines.append("Raw evidence remains under the global Burp run path. Do not read raw/ broadly.")
    write_text(paths["latest"], "\n".join(latest_lines) + "\n")
    handoff_lines = [f"# Burp handoff: {project_related}", "", "Use this as a compact project pointer list. Raw traffic stays global. Tools may scan all related runs or a bounded subset depending on the task.", "", "## Runs"]
    for item in latest:
        handoff_lines.append(f"- `{item.get('run_id')}` ({item.get('source_type')}): status={item.get('status')}, rows={item.get('rows_total')}, global_path=`{item.get('global_run_path')}`")
    handoff_lines.extend(["", "## RAG policy", "- Index runs.jsonl, latest.md, and this handoff.md.", "- Do not index raw/*.mcp.json, redacted/*.txt, or extracted/*.http by default.", ""])
    write_text(paths["handoff"], "\n".join(handoff_lines))


def load_run_manifest(run_dir: Path) -> dict[str, Any]:
    p = run_dir / "run-manifest.json"
    return read_json(p) if p.exists() else {"run_id": run_dir.name}


def save_result(run_dir: Path, label: str, result: dict[str, Any], source_type: str, target: str, project_related: str = "", tags: list[str] | None = None) -> list[dict[str, Any]]:
    raw = run_dir / "raw" / f"{label}.mcp.json"
    red = run_dir / "redacted" / f"{label}.txt"
    write_json(raw, result, mode=0o600)
    text = "\n\n--- MCP_TEXT_PART ---\n\n".join(extract_texts(result))
    write_text(red, redact(text), mode=0o600)
    rows = rows_from_result(result, run_dir.name, source_type, str(raw), target=target, project_related=project_related, tags=tags)
    return rows


def rebuild(run_dir: Path) -> dict[str, Any]:
    rows = read_jsonl(run_dir / "requests.jsonl")
    endpoints = Counter(f"{r.get('method','')} {r.get('host','')}{r.get('path','')}".strip() or "<unparsed>" for r in rows)
    auths = Counter(str(r.get("auth_header_type")) for r in rows if r.get("auth_header_type"))
    cookies = Counter(c for r in rows for c in r.get("cookie_names", []) if c)
    sens = Counter(c for r in rows for c in r.get("sensitive_cookie_name_hints", []) if c)
    setcookies = Counter(c for r in rows for c in r.get("set_cookie_names", []) if c)
    params = Counter(p for r in rows for g in ("query_param_names", "form_param_names", "json_top_level_keys") for p in r.get(g, []) if p)
    cats = Counter(str(r.get("category") or "unknown") for r in rows)
    statuses = Counter(str(r.get("status_code")) for r in rows if r.get("status_code") is not None)
    notes = []
    for r in rows:
        if r.get("interesting_notes"):
            notes.append(f"- {r.get('source_type')}#{r.get('burp_id', r.get('source_object_index'))}: `{r.get('method')} {r.get('host')}{r.get('path')}` — {', '.join(map(str, r.get('interesting_notes', [])))}")
    write_text(run_dir / "endpoints.md", "# Endpoints\n\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in endpoints.most_common()) or "- None") + "\n")
    write_text(run_dir / "auth-cookies.md", "# Auth and cookies\n\n## Authorization header types\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in auths.most_common()) or "- None") + "\n\n## Cookie names\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in cookies.most_common()) or "- None") + "\n\n## Sensitive cookie name hints\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in sens.most_common()) or "- None") + "\n\n## Set-Cookie names\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in setcookies.most_common()) or "- None") + "\n")
    write_text(run_dir / "variables.md", "# Variables and parameters\n\n" + ("\n".join(f"- `{redact(k)}` — {v}" for k, v in params.most_common()) or "- None") + "\n")
    write_text(run_dir / "interesting.md", "# Interesting observations\n\n## Categories\n" + ("\n".join(f"- `{k}` — {v}" for k, v in cats.most_common()) or "- None") + "\n\n## Status codes\n" + ("\n".join(f"- `{k}` — {v}" for k, v in statuses.most_common()) or "- None") + "\n\n## Notes\n" + ("\n".join(redact(n) for n in notes[:1000]) or "- None") + "\n")
    write_text(run_dir / "handoff.md", f"# Handoff\n\n- run_id: {run_dir.name}\n- updated_at: {now()}\n- rows_total: {len(rows)}\n- unparsed_rows: {sum(1 for r in rows if r.get('parse_status') == 'raw_saved_unparsed')}\n- compact_files: requests.jsonl, endpoints.md, auth-cookies.md, variables.md, interesting.md\n\nUse compact files first. Do not read raw/ broadly; inspect one raw evidence file only when required.\n")
    manifest = load_run_manifest(run_dir)
    manifest.update({"updated_at": now(), "status": "indexed", "rows_total": len(rows), "compact_files": ["requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"]})
    write_json(run_dir / "run-manifest.json", manifest)
    update_project_burp_pointer(run_dir, rows_total=len(rows), status=str(manifest.get("status", "indexed")))
    idx = burp_root() / "evidence-index.md"
    if not idx.exists():
        write_text(idx, "# Burp evidence index\n\n")
    with idx.open("a", encoding="utf-8") as f:
        f.write(f"- {now()} run=`{run_dir.name}` rows={len(rows)} path=`{run_dir}`\n")
    return {"status": "rebuilt", "run_dir": str(run_dir), "rows": len(rows)}


def append_rows(run_dir: Path, rows: list[dict[str, Any]]) -> None:
    existing = read_jsonl(run_dir / "requests.jsonl")
    combined = existing + rows
    # Prefer dedup only for stable non-null keys. Active editor snapshots should keep multiple rows.
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for r in combined:
        key = (str(r.get("source_type", "")), str(r.get("burp_id") or ""), str(r.get("method", "")), str(r.get("host", "") + r.get("path", "")))
        if r.get("burp_id") is not None and key in seen:
            continue
        if r.get("burp_id") is not None:
            seen.add(key)
        out.append(r)
    write_jsonl(run_dir / "requests.jsonl", out)


def mcp_tools(target: str = DEFAULT_TARGET, save_to: Path | None = None) -> dict[str, Any]:
    c = client_for_target(target)
    result = c.tools()
    tools = tool_list(result)
    if save_to:
        ensure_dir(save_to)
        write_json(save_to / "mcp-tools.json", result)
        lines = ["# Burp MCP tools", "", f"- target: {target}", f"- generated_at: {now()}", ""]
        for t in tools:
            lines.extend([f"## {t.get('name', '<unnamed>')}", "", str(t.get("description", "")), "", "```json", json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2), "```", ""])
        write_text(save_to / "mcp-tools.md", "\n".join(lines))
    return {"status": "ok", "target": target, "tool_count": len(tools), "tools": tools}


def resolve_source_tool(client: McpSseClient, source: str, target: str) -> tuple[str, dict[str, Any]]:
    profile = load_profile()
    source_cfg = profile.get("sources", {}).get(source)
    if not source_cfg:
        raise SystemExit(f"Unknown Burp source {source}. Edit {profile_path()}.")
    tools = tool_list(client.tools())
    alias = source_cfg.get("tool_alias", source)
    tool_name = source_cfg.get("tool_name") or discover_tool_name(tools, alias)
    if not tool_name:
        available = ", ".join(t.get("name", "") for t in tools)
        raise SystemExit(f"Could not discover Burp tool for source={source} alias={alias}. Available: {available}")
    return str(tool_name), source_cfg


def pull_source(source: str, target: str = DEFAULT_TARGET, count: int = 50, offset: int = 0, pages: int = 1, regex: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    ensure_store()
    rd = Path(run_dir) if run_dir else create_run(source, target=target, project_related=project_related, tags=tags)
    c = client_for_target(target)
    tool_name, source_cfg = resolve_source_tool(c, source, target)
    all_rows: list[dict[str, Any]] = []
    for page in range(max(1, pages)):
        ctx = {"count": count, "offset": offset + page * count, "regex": regex, "target": target, "source": source}
        args = render_template(source_cfg.get("arguments", {}), ctx)
        if not isinstance(args, dict):
            raise SystemExit("Rendered Burp source arguments must be a JSON object")
        result = c.call_tool(tool_name, args, DEFAULT_CALL_TIMEOUT)
        label = f"{source}-{clean(target)}-offset-{ctx['offset']}-count-{count}"
        rows = save_result(rd, label, result, source_type=source, target=target, project_related=project_related, tags=tags)
        all_rows.extend(rows)
    append_rows(rd, all_rows)
    rebuilt = rebuild(rd)
    return {"status": "pulled", "run_dir": str(rd), "source": source, "target": target, "tool_name": tool_name, "rows_added": len(all_rows), "inventory": rebuilt}



def parse_header_options(header_values: list[str] | None) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for item in header_values or []:
        if ":" not in item:
            raise SystemExit(f"Header must be 'Name: value', got: {item!r}")
        name, value = item.split(":", 1)
        name = name.strip()
        if not name:
            raise SystemExit(f"Header name cannot be empty: {item!r}")
        headers.append((name, value.strip()))
    return headers


def body_from_args(body: str = "", body_file: str = "") -> str:
    if body_file:
        return read_text(Path(body_file))
    return body or ""


def request_target_from_url(url: str) -> tuple[str, str, int, bool]:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit("URL must include scheme and host, for example https://example.test/path")
    uses_https = parsed.scheme.lower() == "https"
    port = parsed.port or (443 if uses_https else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    hostname = parsed.hostname or ""
    return path, hostname, port, uses_https


def build_raw_http1_request(method: str, url: str, headers: list[tuple[str, str]] | None = None, body: str = "") -> tuple[str, str, int, bool]:
    path, hostname, port, uses_https = request_target_from_url(url)
    header_list = list(headers or [])
    if not any(name.lower() == "host" for name, _ in header_list):
        host_value = hostname if (uses_https and port == 443) or ((not uses_https) and port == 80) else f"{hostname}:{port}"
        header_list.insert(0, ("Host", host_value))
    if body and not any(name.lower() == "content-length" for name, _ in header_list):
        header_list.append(("Content-Length", str(len(body.encode("utf-8")))))
    lines = [f"{method.upper()} {path} HTTP/1.1"] + [f"{name}: {value}" for name, value in header_list]
    return "\r\n".join(lines) + "\r\n\r\n" + body, hostname, port, uses_https


def infer_service_from_raw_request(content: str, target_hostname: str = "", target_port: int | None = None, uses_https: bool | None = None, url: str = "") -> tuple[str, int, bool]:
    if url:
        _, h, p, https = request_target_from_url(url)
        return target_hostname or h, target_port or p, uses_https if uses_https is not None else https
    head, _ = split_head_body(content)
    headers, _ = parse_headers(head.splitlines()[1:])
    host = target_hostname or headers.get("host", "")
    if not host:
        raise SystemExit("Cannot infer targetHostname: provide --url or --target-hostname or a Host header")
    if host.startswith("[") and "]" in host:
        hostname = host[1:host.index("]")]
        maybe_port = host[host.index("]") + 1:]
        inferred_port = int(maybe_port[1:]) if maybe_port.startswith(":") and maybe_port[1:].isdigit() else None
    elif ":" in host and host.rsplit(":", 1)[1].isdigit():
        hostname, port_s = host.rsplit(":", 1)
        inferred_port = int(port_s)
    else:
        hostname = host
        inferred_port = None
    https = bool(uses_https) if uses_https is not None else False
    port = target_port or inferred_port or (443 if https else 80)
    return hostname, int(port), https


def resolve_action_tool(client: McpSseClient, action: str) -> str:
    profile = load_profile()
    action_cfg = profile.get("actions", {}).get(action, {})
    tools = tool_list(client.tools())
    alias = action_cfg.get("tool_alias", action)
    tool_name = action_cfg.get("tool_name") or discover_tool_name(tools, alias)
    if not tool_name:
        available = ", ".join(str(t.get("name", "")) for t in tools)
        raise SystemExit(f"Could not discover Burp action tool for action={action} alias={alias}. Available: {available}")
    return str(tool_name)


def response_text_from_tool_result(result: dict[str, Any]) -> str:
    texts = extract_texts(result)
    if texts:
        return "\n\n".join(texts)
    return json.dumps(result, ensure_ascii=False, sort_keys=True)


def save_active_result(run_dir: Path, label: str, result: dict[str, Any], source_type: str, target: str, raw_request: str, response_text: str = "", project_related: str = "", tags: list[str] | None = None, tool_name: str = "") -> dict[str, Any]:
    raw = run_dir / "raw" / f"{label}.mcp.json"
    red = run_dir / "redacted" / f"{label}.txt"
    evidence = {
        "schema": "awoki-burp-active-action-v1",
        "created_at": now(),
        "source_type": source_type,
        "target": target,
        "tool_name": tool_name,
        "request": raw_request,
        "response_text": response_text,
        "tool_result": result,
    }
    write_json(raw, evidence, mode=0o600)
    write_text(red, redact("--- REQUEST ---\n" + raw_request + "\n\n--- RESPONSE / TOOL RESULT ---\n" + response_text), mode=0o600)
    row = parse_http_pair(raw_request, response_text, run_dir.name, source_type, str(raw), burp_id=None, idx=0, target=target, project_related=project_related, tags=tags)
    row["tool_name"] = tool_name
    row["action_status"] = "sent" if source_type.startswith("send") else "created"
    row["evidence_file_if_saved"] = str(raw)
    append_rows(run_dir, [row])
    rebuilt = rebuild(run_dir)
    return {"status": "ok", "run_dir": str(run_dir), "source_type": source_type, "tool_name": tool_name, "evidence_file": str(raw), "redacted_file": str(red), "inventory": rebuilt}


def active_run(source_type: str, target: str, project_related: str, tags: list[str] | None, run_dir: str = "") -> Path:
    return Path(run_dir) if run_dir else create_run(source_type, target=target, project_related=project_related, tags=tags)


def send_raw_http1(content: str, target: str = DEFAULT_TARGET, target_hostname: str = "", target_port: int | None = None, uses_https: bool | None = None, url: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    rd = active_run("send_http1", target, project_related, tags, run_dir)
    c = client_for_target(target)
    tool_name = resolve_action_tool(c, "send_http1")
    hostname, port, https = infer_service_from_raw_request(content, target_hostname=target_hostname, target_port=target_port, uses_https=uses_https, url=url)
    args = {"content": content, "targetHostname": hostname, "targetPort": int(port), "usesHttps": bool(https)}
    result = c.call_tool(tool_name, args, DEFAULT_CALL_TIMEOUT)
    response = response_text_from_tool_result(result)
    return save_active_result(rd, f"send-http1-{clean(hostname)}-{int(time.time())}", result, "send_http1", target, content, response, project_related, tags, tool_name)


def send_request(method: str, url: str, headers: list[tuple[str, str]] | None = None, body: str = "", target: str = DEFAULT_TARGET, project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    content, hostname, port, https = build_raw_http1_request(method, url, headers=headers, body=body)
    return send_raw_http1(content, target=target, target_hostname=hostname, target_port=port, uses_https=https, url=url, project_related=project_related, tags=tags, run_dir=run_dir)


def create_repeater_from_raw(content: str, target: str = DEFAULT_TARGET, target_hostname: str = "", target_port: int | None = None, uses_https: bool | None = None, url: str = "", tab_name: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    rd = active_run("create_repeater", target, project_related, tags, run_dir)
    c = client_for_target(target)
    tool_name = resolve_action_tool(c, "create_repeater")
    hostname, port, https = infer_service_from_raw_request(content, target_hostname=target_hostname, target_port=target_port, uses_https=uses_https, url=url)
    args = {"tabName": tab_name or None, "content": content, "targetHostname": hostname, "targetPort": int(port), "usesHttps": bool(https)}
    result = c.call_tool(tool_name, args, DEFAULT_CALL_TIMEOUT)
    response = response_text_from_tool_result(result)
    return save_active_result(rd, f"repeater-{clean(hostname)}-{int(time.time())}", result, "create_repeater", target, content, response, project_related, tags, tool_name)


def send_to_intruder_from_raw(content: str, target: str = DEFAULT_TARGET, target_hostname: str = "", target_port: int | None = None, uses_https: bool | None = None, url: str = "", tab_name: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    rd = active_run("send_to_intruder", target, project_related, tags, run_dir)
    c = client_for_target(target)
    tool_name = resolve_action_tool(c, "send_to_intruder")
    hostname, port, https = infer_service_from_raw_request(content, target_hostname=target_hostname, target_port=target_port, uses_https=uses_https, url=url)
    args = {"tabName": tab_name or None, "content": content, "targetHostname": hostname, "targetPort": int(port), "usesHttps": bool(https)}
    result = c.call_tool(tool_name, args, DEFAULT_CALL_TIMEOUT)
    response = response_text_from_tool_result(result)
    return save_active_result(rd, f"intruder-{clean(hostname)}-{int(time.time())}", result, "send_to_intruder", target, content, response, project_related, tags, tool_name)


def get_active_editor_text(target: str = DEFAULT_TARGET) -> tuple[str, str, dict[str, Any]]:
    c = client_for_target(target)
    tool_name = resolve_action_tool(c, "active_editor")
    result = c.call_tool(tool_name, {}, DEFAULT_CALL_TIMEOUT)
    return response_text_from_tool_result(result), tool_name, result


def set_active_editor_text(text: str, target: str = DEFAULT_TARGET) -> dict[str, Any]:
    c = client_for_target(target)
    tool_name = resolve_action_tool(c, "set_active_editor")
    result = c.call_tool(tool_name, {"text": text}, DEFAULT_CALL_TIMEOUT)
    return {"status": "ok", "target": target, "tool_name": tool_name, "result_preview": redact(response_text_from_tool_result(result))[:1000]}


def active_to_repeater(target: str = DEFAULT_TARGET, tab_name: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    text, _, _ = get_active_editor_text(target)
    if not text.strip():
        raise SystemExit("Active Burp editor is empty or unavailable. Focus a request editor in Burp first.")
    return create_repeater_from_raw(text, target=target, tab_name=tab_name, project_related=project_related, tags=tags, run_dir=run_dir)


def active_to_intruder(target: str = DEFAULT_TARGET, tab_name: str = "", project_related: str = "", tags: list[str] | None = None, run_dir: str = "") -> dict[str, Any]:
    text, _, _ = get_active_editor_text(target)
    if not text.strip():
        raise SystemExit("Active Burp editor is empty or unavailable. Focus a request editor in Burp first.")
    return send_to_intruder_from_raw(text, target=target, tab_name=tab_name, project_related=project_related, tags=tags, run_dir=run_dir)



def raw_request_from_evidence(row: dict[str, Any]) -> str:
    path_value = row.get("evidence_file_if_saved") or row.get("_source_file") or ""
    if not path_value:
        raise SystemExit("Selected Burp row has no evidence file")
    p = Path(str(path_value))
    if not p.exists():
        raise SystemExit(f"Evidence file does not exist: {p}")
    obj = read_json(p)
    if isinstance(obj, dict) and isinstance(obj.get("request"), str):
        return normalize_http_text_for_parse(obj["request"])
    candidates: list[tuple[Any, int, str, str]] = []
    for candidate in flatten_json_candidates(obj):
        if not isinstance(candidate, dict):
            continue
        req, _ = normalize_request_response(candidate)
        if not req:
            continue
        bid = candidate.get("id") or candidate.get("burp_id") or candidate.get("history_id") or candidate.get("tool_id") or candidate.get("message_id")
        idx = candidate.get("source_object_index") if isinstance(candidate.get("source_object_index"), int) else -1
        candidates.append((bid, idx, req, json.dumps(candidate, ensure_ascii=False, default=str)[:500]))
    wanted = row.get("burp_id")
    wanted_idx = row.get("source_object_index")
    for bid, idx, req, _ in candidates:
        if wanted is not None and str(bid) == str(wanted):
            return req
        if wanted is None and wanted_idx is not None and idx == wanted_idx:
            return req
    method, host, path = row.get("method"), row.get("host"), row.get("path")
    for _, _, req, _ in candidates:
        parsed = parse_http_pair(req, "", row.get("run_id", ""), row.get("source_type", "history"), str(p))
        if parsed.get("method") == method and parsed.get("host") == host and parsed.get("path") == path:
            return req
    if candidates:
        return candidates[0][2]
    texts = extract_texts(obj)
    for text in texts:
        matches = parse_text_http_messages(text, row.get("run_id", ""), row.get("source_type", "history"), str(p))
        if matches:
            return normalize_http_text_for_parse(text)
    raise SystemExit(f"Could not recover raw request from evidence file: {p}")

def find_inventory_row(burp_id: str | int, run_dir: str = "", source_type: str = "") -> dict[str, Any]:
    rd = Path(run_dir) if run_dir else latest_run_dir()
    rows = read_jsonl(rd / "requests.jsonl")
    matches = [r for r in rows if str(r.get("burp_id")) == str(burp_id) and (not source_type or r.get("source_type") == source_type)]
    if not matches:
        raise SystemExit(f"No Burp inventory row found for burp_id={burp_id} in {rd}")
    if len(matches) > 1:
        raise SystemExit(f"Multiple Burp rows match burp_id={burp_id}; pass --source-type or --run-dir")
    return matches[0]


def history_to_repeater(burp_id: str | int, target: str = DEFAULT_TARGET, run_dir: str = "", source_type: str = "", tab_name: str = "", project_related: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    row = find_inventory_row(burp_id, run_dir=run_dir, source_type=source_type)
    req = raw_request_from_evidence(row)
    return create_repeater_from_raw(req, target=target, tab_name=tab_name or f"history-{burp_id}", project_related=project_related or str(row.get("project_related", "")), tags=tags or row.get("tags", []), run_dir=run_dir)


def history_to_intruder(burp_id: str | int, target: str = DEFAULT_TARGET, run_dir: str = "", source_type: str = "", tab_name: str = "", project_related: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    row = find_inventory_row(burp_id, run_dir=run_dir, source_type=source_type)
    req = raw_request_from_evidence(row)
    return send_to_intruder_from_raw(req, target=target, tab_name=tab_name or f"history-{burp_id}", project_related=project_related or str(row.get("project_related", "")), tags=tags or row.get("tags", []), run_dir=run_dir)


def raw_request_from_cli_args(a: argparse.Namespace) -> str:
    if getattr(a, "raw_file", ""):
        return read_text(Path(a.raw_file))
    if getattr(a, "raw", ""):
        return str(a.raw)
    if getattr(a, "url", ""):
        content, _, _, _ = build_raw_http1_request(getattr(a, "method", "GET"), a.url, headers=parse_header_options(getattr(a, "header", [])), body=body_from_args(getattr(a, "body", ""), getattr(a, "body_file", "")))
        return content
    raise SystemExit("Provide --raw-file, --raw, or --url")




def find_inventory_row_by_pattern(pattern: str, run_dir: str = "", source_type: str = "") -> dict[str, Any]:
    rd = Path(run_dir) if run_dir else latest_run_dir()
    rows = read_jsonl(rd / "requests.jsonl")
    matches = []
    for r in rows:
        if source_type and r.get("source_type") != source_type:
            continue
        if row_matches_pattern(r, pattern, include_raw=True):
            matches.append(r)
    if not matches:
        raise SystemExit(f"No Burp inventory row matched pattern={pattern!r} in {rd}")
    return matches[0]

def run_dir_by_id(run_id_value: str) -> Path:
    candidate = runs_dir() / clean(run_id_value)
    if candidate.exists():
        return candidate
    # Do not over-clean if project names use underscores/colons that clean preserved differently.
    raw_candidate = runs_dir() / str(run_id_value)
    if raw_candidate.exists():
        return raw_candidate
    raise SystemExit(f"No Burp run directory found for run_id={run_id_value}")


def iter_run_dirs(project_related: str = "", limit: int = 0, all_runs: bool = False) -> list[Path]:
    ensure_store()
    if not runs_dir().exists():
        return []
    project_filter = project_slug(project_related) if project_related else ""
    dirs = sorted([p for p in runs_dir().iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)
    out: list[Path] = []
    for rd in dirs:
        manifest = load_run_manifest(rd)
        prefix = str(manifest.get("project_prefix") or project_slug(str(manifest.get("project_related", ""))))
        project = str(manifest.get("project_related", "") or "")
        if project_filter and project_filter not in {prefix, project_slug(project)}:
            continue
        out.append(rd)
        if not all_runs and limit and limit > 0 and len(out) >= limit:
            break
    return out


def burp_run_list(project_related: str = "", limit: int = 0, all_runs: bool = False) -> list[dict[str, Any]]:
    rows = []
    for rd in iter_run_dirs(project_related=project_related, limit=limit, all_runs=all_runs):
        manifest = load_run_manifest(rd)
        req_rows = read_jsonl(rd / "requests.jsonl")
        rows.append({
            "run_id": rd.name,
            "created_at": manifest.get("created_at", ""),
            "updated_at": manifest.get("updated_at", ""),
            "source_type": manifest.get("source_type", ""),
            "project_related": manifest.get("project_related", ""),
            "project_prefix": manifest.get("project_prefix", ""),
            "status": manifest.get("status", ""),
            "rows_total": len(req_rows),
            "global_run_path": str(rd),
            "compact_files": [str(rd / name) for name in ["requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"] if (rd / name).exists()],
            "raw_policy": "raw evidence remains global; do not load raw/ broadly",
        })
    return rows


def burp_run_summary(run_id_value: str = "", run_dir: str = "", preview: int = 10) -> dict[str, Any]:
    rd = Path(run_dir) if run_dir else (run_dir_by_id(run_id_value) if run_id_value else latest_run_dir())
    manifest = load_run_manifest(rd)
    rows = read_jsonl(rd / "requests.jsonl")
    return {
        "status": "ok",
        "run_id": rd.name,
        "global_run_path": str(rd),
        "manifest": manifest,
        "rows_total": len(rows),
        "preview": [compact_request_row(r) for r in rows[:max(0, preview)]],
        "compact_files": {
            name: str(rd / name) for name in ["requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"] if (rd / name).exists()
        },
        "raw_policy": "Do not read raw/ broadly. Use burp_find_request, burp_show_request, or burp_extract_request.",
    }


def compact_request_row(row: dict[str, Any]) -> dict[str, Any]:
    ref = request_ref(row)
    return {
        "request_ref": ref,
        "run_id": row.get("run_id", ""),
        "burp_id": row.get("burp_id"),
        "source_object_index": row.get("source_object_index"),
        "source_type": row.get("source_type", ""),
        "method": row.get("method", ""),
        "host": row.get("host", ""),
        "path": row.get("path", ""),
        "status_code": row.get("status_code"),
        "content_type": row.get("content_type", ""),
        "category": row.get("category", ""),
        "interesting_notes": row.get("interesting_notes", [])[:5] if isinstance(row.get("interesting_notes", []), list) else [],
        "evidence_file_if_saved": row.get("evidence_file_if_saved", ""),
        "parse_status": row.get("parse_status", ""),
    }


def request_ref(row: dict[str, Any]) -> str:
    ident = row.get("burp_id")
    if ident is None or ident == "":
        ident = f"idx-{row.get('source_object_index', 0)}"
    return f"{row.get('run_id', '')}:req:{ident}"


def parse_request_ref(ref: str) -> tuple[str, str]:
    parts = str(ref).split(":req:", 1)
    if len(parts) != 2:
        raise SystemExit("request_ref must look like <run_id>:req:<burp_id-or-idx-N>")
    return parts[0], parts[1]


def row_matches_pattern(row: dict[str, Any], pattern: str, include_raw: bool = False) -> bool:
    rx = re.compile(pattern, re.I)
    hay = " ".join(str(row.get(k, "")) for k in ["method", "host", "path", "status_code", "content_type", "category", "parse_status"])
    hay += " " + " ".join(map(str, row.get("interesting_notes", [])))
    hay += " " + " ".join(map(str, row.get("query_param_names", [])))
    hay += " " + " ".join(map(str, row.get("form_param_names", [])))
    if rx.search(hay):
        return True
    if include_raw:
        try:
            raw = raw_request_from_evidence(row)
            return bool(rx.search(raw))
        except Exception:
            return False
    return False


def find_rows_by_pattern(pattern: str, project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0, max_matches: int = 10, include_raw: bool = True) -> tuple[list[dict[str, Any]], list[Path]]:
    if run_dir:
        dirs = [Path(run_dir)]
    elif run_id_value:
        dirs = [run_dir_by_id(run_id_value)]
    else:
        dirs = iter_run_dirs(project_related=project_related, limit=limit_runs, all_runs=all_runs)
    matches: list[dict[str, Any]] = []
    for rd in dirs:
        rows = read_jsonl(rd / "requests.jsonl")
        for row in rows:
            if row_matches_pattern(row, pattern, include_raw=include_raw):
                row = dict(row)
                row.setdefault("run_id", rd.name)
                matches.append(row)
                if len(matches) >= max_matches:
                    return matches, dirs
    return matches, dirs


def find_inventory_row_by_ref(request_ref_value: str) -> dict[str, Any]:
    rid, ident = parse_request_ref(request_ref_value)
    rd = run_dir_by_id(rid)
    rows = read_jsonl(rd / "requests.jsonl")
    for row in rows:
        if ident.startswith("idx-") and str(row.get("source_object_index")) == ident[4:]:
            return row
        if str(row.get("burp_id")) == ident:
            return row
    raise SystemExit(f"No row found for request_ref={request_ref_value}")


def burp_find_request(pattern: str, project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0, max_matches: int = 10) -> dict[str, Any]:
    matches, searched = find_rows_by_pattern(pattern, project_related=project_related, run_dir=run_dir, run_id_value=run_id_value, all_runs=all_runs, limit_runs=limit_runs, max_matches=max_matches, include_raw=True)
    return {
        "status": "ok" if matches else "no_matches",
        "pattern": pattern,
        "project_related": project_related,
        "searched_runs": [p.name for p in searched],
        "searched_policy": "no fixed run limit by default; use --limit-runs N only for a bounded preview",
        "match_count": len(matches),
        "matches": [compact_request_row(r) for r in matches],
        "next_action": "Use burp_show_request with request_ref, burp_extract_request, or create a Repeater tab from the selected request." if matches else "Run a new Burp pull, simplify the pattern, or specify a run_id/run_dir if you know where the evidence lives.",
        "retryable": False,
    }


def burp_show_request(request_ref_value: str = "", pattern: str = "", project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0, max_bytes: int = 6000) -> dict[str, Any]:
    if request_ref_value:
        row = find_inventory_row_by_ref(request_ref_value)
    elif pattern:
        matches, _ = find_rows_by_pattern(pattern, project_related=project_related, run_dir=run_dir, run_id_value=run_id_value, all_runs=all_runs, limit_runs=limit_runs, max_matches=1, include_raw=True)
        if not matches:
            raise SystemExit(f"No request matched pattern={pattern!r}")
        row = matches[0]
    else:
        raise SystemExit("Provide --request-ref or --pattern")
    raw = raw_request_from_evidence(row)
    red = redact(raw)
    return {
        "status": "ok",
        "request_ref": request_ref(row),
        "row": compact_request_row(row),
        "request_preview_redacted": red[:max_bytes],
        "truncated": len(red) > max_bytes,
        "raw_policy": "Preview is redacted. Use burp_extract_request for a deliberate .http artifact.",
    }



def select_row_for_request(pattern: str = "", request_ref_value: str = "", project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0) -> tuple[dict[str, Any], Path]:
    if request_ref_value:
        row = find_inventory_row_by_ref(request_ref_value)
        rid, _ = parse_request_ref(request_ref_value)
        return row, run_dir_by_id(rid)
    if pattern:
        if run_dir:
            rd = Path(run_dir)
            return find_inventory_row_by_pattern(pattern, run_dir=str(rd)), rd
        if run_id_value:
            rd = run_dir_by_id(run_id_value)
            return find_inventory_row_by_pattern(pattern, run_dir=str(rd)), rd
        matches, searched = find_rows_by_pattern(pattern, project_related=project_related, all_runs=all_runs, limit_runs=limit_runs, max_matches=1, include_raw=True)
        if not matches:
            raise SystemExit(f"No request matched pattern={pattern!r} in searched runs: {', '.join(p.name for p in searched)}")
        return matches[0], run_dir_by_id(str(matches[0].get("run_id")))
    raise SystemExit("Provide --request-ref or --pattern")


def burp_request_to_repeater(request_ref_value: str = "", pattern: str = "", target: str = DEFAULT_TARGET, tab_name: str = "", project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    row, rd = select_row_for_request(pattern=pattern, request_ref_value=request_ref_value, project_related=project_related, run_dir=run_dir, run_id_value=run_id_value, all_runs=all_runs, limit_runs=limit_runs)
    req = raw_request_from_evidence(row)
    return create_repeater_from_raw(req, target=target, tab_name=tab_name or f"{row.get('method','REQ')} {row.get('path','')}", project_related=project_related or str(row.get("project_related", "")), tags=row.get("tags", []), run_dir=str(rd))


def burp_request_to_intruder(request_ref_value: str = "", pattern: str = "", target: str = DEFAULT_TARGET, tab_name: str = "", project_related: str = "", run_dir: str = "", run_id_value: str = "", all_runs: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    row, rd = select_row_for_request(pattern=pattern, request_ref_value=request_ref_value, project_related=project_related, run_dir=run_dir, run_id_value=run_id_value, all_runs=all_runs, limit_runs=limit_runs)
    req = raw_request_from_evidence(row)
    return send_to_intruder_from_raw(req, target=target, tab_name=tab_name or f"{row.get('method','REQ')} {row.get('path','')}", project_related=project_related or str(row.get("project_related", "")), tags=row.get("tags", []), run_dir=str(rd))


def default_extract_name(row: dict[str, Any], name: str = "") -> str:
    if name:
        return clean(name).removesuffix(".http") + ".http"
    host = clean(str(row.get("host") or "host"))
    path = clean(str(row.get("path") or "request").strip("/") or "root")
    bid = clean(str(row.get("burp_id") or row.get("source_object_index") or "0"))
    return f"{host}__{path}__{bid}.http"



def extract_request(pattern: str = "", burp_id: str = "", request_ref_value: str = "", run_dir: str = "", run_id_value: str = "", source_type: str = "", output: str = "", name: str = "", project_related: str = "", all_runs: bool = False, limit_runs: int = 0) -> dict[str, Any]:
    if request_ref_value:
        row = find_inventory_row_by_ref(request_ref_value)
        rid, _ = parse_request_ref(request_ref_value)
        rd = run_dir_by_id(rid)
    else:
        rd = Path(run_dir) if run_dir else (run_dir_by_id(run_id_value) if run_id_value else latest_run_dir())
        if burp_id:
            row = find_inventory_row(burp_id, run_dir=str(rd), source_type=source_type)
        elif pattern:
            if run_dir or run_id_value:
                row = find_inventory_row_by_pattern(pattern, run_dir=str(rd), source_type=source_type)
            else:
                matches, searched = find_rows_by_pattern(pattern, project_related=project_related, all_runs=all_runs, limit_runs=limit_runs, max_matches=1, include_raw=True)
                if not matches:
                    raise SystemExit(f"No Burp inventory row matched pattern={pattern!r} in searched runs: {', '.join(p.name for p in searched)}")
                row = matches[0]
                rd = run_dir_by_id(str(row.get("run_id")))
        else:
            raise SystemExit("Provide --request-ref, --burp-id, or --pattern")
    raw_req = raw_request_from_evidence(row)
    manifest = load_run_manifest(rd)
    effective_project = project_slug(project_related or str(row.get("project_related", "")) or str(manifest.get("project_related", "")))
    if output:
        out = Path(output)
    elif effective_project != "global" and project_burp_paths(effective_project):
        paths = project_burp_paths(effective_project)
        assert paths is not None
        out = paths["extracted"] / default_extract_name(row, name=name)
    else:
        out = rd / "extracted" / default_extract_name(row, name=name)
    write_text(out, raw_req, mode=0o600)
    pointer = {
        "schema": "awoki-burp-extracted-request-v1",
        "created_at": now(),
        "request_ref": request_ref(row),
        "run_id": rd.name,
        "source_type": row.get("source_type", ""),
        "burp_id": row.get("burp_id"),
        "source_object_index": row.get("source_object_index"),
        "project_related": "" if effective_project == "global" else effective_project,
        "method": row.get("method", ""),
        "host": row.get("host", ""),
        "path": row.get("path", ""),
        "output": str(out),
        "global_run_path": str(rd),
        "note": "Deliberate extracted .http working artifact. Raw full Burp evidence remains global.",
    }
    if effective_project != "global":
        paths = project_burp_paths(effective_project)
        if paths is not None:
            append_jsonl(paths["base"] / "extracted.jsonl", pointer)
            update_project_burp_pointer(rd)
    return {
        "status": "extracted",
        "output": str(out),
        "run_dir": str(rd),
        "run_id": rd.name,
        "request_ref": request_ref(row),
        "row": {k: row.get(k) for k in ["source_type", "burp_id", "source_object_index", "method", "host", "path"]},
    }


def normalize_hostname_filter(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        return (parsed.hostname or "").lower().strip(".")
    # Accept host[:port][/path] and strip path/port without treating colons in IPv6 too aggressively.
    raw = raw.split("/", 1)[0]
    if raw.startswith("[") and "]" in raw:
        return raw[1:raw.index("]")].lower().strip(".")
    if ":" in raw and raw.rsplit(":", 1)[1].isdigit():
        raw = raw.rsplit(":", 1)[0]
    return raw.lower().strip(".")


def host_matches_filter(row_host: str, hostname: str) -> bool:
    wanted = normalize_hostname_filter(hostname)
    got = normalize_hostname_filter(row_host)
    if not wanted or not got:
        return False
    return got == wanted or got.endswith("." + wanted)


def iter_report_run_dirs(project_related: str = "", include_global: bool = True, all_projects: bool = False, run_limit: int = 0) -> list[Path]:
    if all_projects:
        dirs = iter_run_dirs("", limit=0, all_runs=True)
    else:
        dirs = []
        if project_related:
            dirs.extend(iter_run_dirs(project_related, limit=0, all_runs=True))
            if include_global:
                dirs.extend(iter_run_dirs("global", limit=0, all_runs=True))
        else:
            dirs.extend(iter_run_dirs("global", limit=0, all_runs=True))
    seen: set[str] = set()
    deduped: list[Path] = []
    for rd in sorted(dirs, key=lambda p: p.name, reverse=True):
        if str(rd) in seen:
            continue
        seen.add(str(rd))
        deduped.append(rd)
        if run_limit and run_limit > 0 and len(deduped) >= run_limit:
            break
    return deduped


def md_escape_tick(value: Any) -> str:
    return str(value if value is not None else "").replace("`", "\\`")


def burp_host_report(hostname: str, project_related: str = "", include_global: bool = True, all_projects: bool = False, run_limit: int = 0, max_items: int = 100, write_artifacts: bool = True) -> dict[str, Any]:
    wanted = normalize_hostname_filter(hostname)
    if not wanted:
        raise SystemExit("hostname is required")
    dirs = iter_report_run_dirs(project_related=project_related, include_global=include_global, all_projects=all_projects, run_limit=run_limit)
    matches: list[dict[str, Any]] = []
    run_counts: Counter[str] = Counter()
    endpoint_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    content_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    cookie_counts: Counter[str] = Counter()
    set_cookie_counts: Counter[str] = Counter()
    auth_counts: Counter[str] = Counter()
    param_counts: Counter[str] = Counter()
    js_paths: Counter[str] = Counter()
    interesting: list[str] = []
    rows_scanned = 0
    for rd in dirs:
        rows = read_jsonl(rd / "requests.jsonl")
        rows_scanned += len(rows)
        for row in rows:
            if not host_matches_filter(str(row.get("host", "")), wanted):
                continue
            row = dict(row)
            row.setdefault("run_id", rd.name)
            matches.append(row)
            run_counts[rd.name] += 1
            endpoint_key = f"{row.get('method','')} {row.get('path','')}".strip()
            if endpoint_key:
                endpoint_counts[endpoint_key] += 1
            if row.get("status_code") is not None:
                status_counts[str(row.get("status_code"))] += 1
            if row.get("method"):
                method_counts[str(row.get("method"))] += 1
            if row.get("content_type"):
                content_counts[str(row.get("content_type"))[:100]] += 1
            if row.get("category"):
                category_counts[str(row.get("category"))] += 1
            for c in row.get("cookie_names", []) or []:
                cookie_counts[str(c)] += 1
            for c in row.get("set_cookie_names", []) or []:
                set_cookie_counts[str(c)] += 1
            if row.get("auth_header_type"):
                auth_counts[str(row.get("auth_header_type"))] += 1
            for p in (row.get("query_param_names", []) or []) + (row.get("form_param_names", []) or []) + (row.get("json_top_level_keys", []) or []):
                param_counts[str(p)] += 1
            path = str(row.get("path", ""))
            if path.lower().split("?", 1)[0].endswith(".js"):
                js_paths[path] += 1
            for note in row.get("interesting_notes", []) or []:
                note_line = f"{row.get('run_id')} {row.get('method')} {row.get('host')}{row.get('path')}: {note}"
                if len(interesting) < max_items:
                    interesting.append(redact(note_line))
    previews = [compact_request_row(r) for r in matches[:max_items]]
    result: dict[str, Any] = {
        "status": "ok" if matches else "no_matches",
        "hostname": wanted,
        "project_related": project_related,
        "scope": {
            "include_global": include_global,
            "all_projects": all_projects,
            "run_limit": run_limit,
            "run_limit_policy": "0 means no fixed run limit; the caller/model may set a positive limit for a bounded preview",
        },
        "runs_considered": len(dirs),
        "runs_scanned": [p.name for p in dirs],
        "rows_scanned": rows_scanned,
        "requests_found": len(matches),
        "returned_request_previews": len(previews),
        "truncated": len(matches) > len(previews),
        "top_runs": run_counts.most_common(20),
        "methods": method_counts.most_common(),
        "statuses": status_counts.most_common(),
        "categories": category_counts.most_common(),
        "content_types": content_counts.most_common(20),
        "top_endpoints": endpoint_counts.most_common(max_items),
        "auth_header_types": auth_counts.most_common(),
        "cookie_names": cookie_counts.most_common(50),
        "set_cookie_names": set_cookie_counts.most_common(50),
        "parameters": param_counts.most_common(100),
        "javascript_paths": js_paths.most_common(50),
        "interesting_notes": interesting,
        "request_previews": previews,
        "next_action": "Use request_ref with burp_show_request/burp_extract_request/request-to-repeater for a selected request." if matches else "No matching inventory rows. Pull fresh Burp history for the hostname or use --all-projects if evidence may be linked to another project.",
        "retryable": False,
        "raw_policy": "Report uses compact inventories only. Do not load raw/ broadly; inspect one selected request through show/extract tools.",
    }
    if write_artifacts:
        slug = clean(wanted)
        if project_related and project_slug(project_related) != "global" and project_burp_paths(project_related):
            paths = project_burp_paths(project_related)
            assert paths is not None
            out_dir = paths["base"] / "host-reports"
        else:
            out_dir = burp_root() / "host-reports"
        ensure_dir(out_dir)
        json_path = out_dir / f"{slug}.json"
        md_path = out_dir / f"{slug}.md"
        write_json(json_path, result, mode=0o600)
        md_lines = [
            f"# Burp host report: {wanted}", "",
            f"- generated_at: {now()}",
            f"- project_related: {project_related or 'global'}",
            f"- runs_considered: {len(dirs)}",
            f"- rows_scanned: {rows_scanned}",
            f"- requests_found: {len(matches)}",
            f"- truncated: {result['truncated']}",
            "", "## Top endpoints",
        ]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in endpoint_counts.most_common(50)] or ["- None"]
        md_lines += ["", "## Statuses"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in status_counts.most_common()] or ["- None"]
        md_lines += ["", "## Auth/session indicators", "### Authorization header types"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in auth_counts.most_common()] or ["- None"]
        md_lines += ["", "### Cookie names"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in cookie_counts.most_common(50)] or ["- None"]
        md_lines += ["", "### Set-Cookie names"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in set_cookie_counts.most_common(50)] or ["- None"]
        md_lines += ["", "## Parameters"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in param_counts.most_common(100)] or ["- None"]
        md_lines += ["", "## JavaScript paths"]
        md_lines += [f"- `{md_escape_tick(k)}` — {v}" for k, v in js_paths.most_common(50)] or ["- None"]
        md_lines += ["", "## Interesting notes"]
        md_lines += [f"- {md_escape_tick(n)}" for n in interesting[:100]] or ["- None"]
        md_lines += ["", "## Request refs"]
        md_lines += [f"- `{md_escape_tick(r.get('request_ref'))}` — `{md_escape_tick(r.get('method'))} {md_escape_tick(r.get('host'))}{md_escape_tick(r.get('path'))}` status={r.get('status_code')}" for r in previews[:100]] or ["- None"]
        md_lines += ["", "Raw evidence remains in global Burp run folders. Use show/extract tools for one selected request."]
        write_text(md_path, "\n".join(md_lines) + "\n", mode=0o600)
        result["artifact_paths"] = {"json": str(json_path), "markdown": str(md_path)}
    return result



def current_session_project_id() -> str:
    """Best-effort session-attached project lookup for Awoki MCP calls.

    This deliberately falls back to empty/global for shell use so new sessions do
    not auto-attach to old projects.
    """
    try:
        import project_workspace
        pid = project_workspace.current_project_id(awoki_root())
        return str(pid or "")
    except Exception:
        return ""


def effective_project_for_save(project_related: str = "") -> str:
    explicit = project_slug(project_related) if str(project_related or "").strip() else ""
    if explicit:
        return explicit
    current = current_session_project_id()
    return project_slug(current) if current else "global"


def ensure_project_burp_workspace(project_related: str) -> dict[str, Path] | None:
    pid = project_slug(project_related)
    if pid == "global":
        return None
    paths = project_burp_paths(pid)
    if paths is None:
        return None
    for key in ["base", "extracted", "host_reports", "tasks"]:
        ensure_dir(paths[key])
    for key in ["runs", "observations", "host_summaries"]:
        if not paths[key].exists():
            write_text(paths[key], "")
    if not paths["latest"].exists():
        write_text(paths["latest"], f"# Latest Burp evidence: {pid}\n\nNo Burp evidence linked yet.\n")
    if not paths["handoff"].exists():
        write_text(paths["handoff"], f"# Burp handoff: {pid}\n\nNo Burp evidence linked yet.\n")
    return paths


def _continuity_adapter_warning(operation: str, exc: Exception) -> str:
    safe_error = redact(str(exc or "continuity adapter failed"))[:500]
    return f"{operation} failed ({type(exc).__name__}): {safe_error}"


def refresh_project_handoff_if_possible(project_related: str) -> str:
    pid = project_slug(project_related)
    if pid == "global":
        return ""
    try:
        import project_workspace
        project_workspace.refresh_project_files(awoki_root(), pid)
        return ""
    except Exception as exc:
        return _continuity_adapter_warning("project handoff refresh", exc)


def append_project_event_if_possible(project_related: str, summary: str, event_type: str = "burp") -> str:
    pid = project_slug(project_related)
    if pid == "global":
        return ""
    try:
        import project_workspace
        project_workspace.project_record_event(awoki_root(), pid, summary=summary, event_type=event_type)
        return ""
    except Exception as exc:
        return _continuity_adapter_warning("project event capture", exc)


def _brief(value: Any, limit: int = 400) -> str:
    text = redact(str(value or "").strip().replace("\n", " "))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def burp_record_observation(
    project: str = "",
    title: str = "",
    summary: str = "",
    host: str = "",
    method: str = "",
    path: str = "",
    status_code: str = "",
    request_ref_value: str = "",
    artifact: str = "",
    next_action: str = "",
    source: str = "burp_mcp",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Record a compact observation produced from direct Burp MCP live work.

    This is the preferred bridge from live PortSwigger MCP operations into
    Awoki's project memory/RAG layer. It does not pull or parse raw history.
    """
    pid = effective_project_for_save(project)
    if not title and not summary:
        raise SystemExit("Provide title or summary for the Burp observation")
    row = {
        "schema": "awoki-burp-observation-v1",
        "kind": "burp_observation",
        "created_at": now(),
        "project_related": "" if pid == "global" else pid,
        "source": source or "burp_mcp",
        "title": _brief(title or summary, 220),
        "summary": _brief(summary or title, 1200),
        "host": normalize_hostname_filter(host) if host else "",
        "method": str(method or "").upper(),
        "path": str(path or ""),
        "status_code": str(status_code or ""),
        "request_ref": str(request_ref_value or ""),
        "artifact": str(artifact or ""),
        "next_action": _brief(next_action, 500),
        "tags": tags or [],
        "raw_policy": "Observation is compact/RAG-safe; raw Burp traffic remains in Burp or explicit artifacts only.",
    }
    if pid == "global":
        out = burp_root() / "observations.jsonl"
        append_jsonl(out, row)
        return {"status": "saved", "scope": "global", "path": str(out), "observation": row}
    paths = ensure_project_burp_workspace(pid)
    assert paths is not None
    append_jsonl(paths["observations"], row)
    continuity_warning = ""
    # Burp is an adapter into the canonical continuity journal. Keep the typed
    # finding only as a linked compatibility projection.
    try:
        import project_workspace
        sources = []
        if row.get("artifact"):
            sources.append({"type": "artifact", "path": row["artifact"]})
        if row.get("request_ref"):
            sources.append({"type": "burp_request_ref", "id": row["request_ref"]})
        if row.get("host"):
            sources.append({"type": "host", "location": row["host"]})
        captured = project_workspace.project_capture(
            awoki_root(),
            pid,
            row["title"],
            kind="observation",
            details=row["summary"],
            sources=sources,
            confidence="medium",
            tags=["burp", "web-security"] + list(tags or []),
            likely_continuation=row.get("next_action", ""),
            metadata={"adapter": "burp", "source": row.get("source")},
            refresh=False,
        )
        project_workspace.append_jsonl(awoki_root() / "workspace" / "projects" / pid / "memory" / "findings.jsonl", {
            "kind": "finding",
            "continuity_id": captured.get("id"),
            "source": "burp",
            "title": row["title"],
            "summary": row["summary"],
            "evidence": row.get("artifact") or row.get("request_ref") or row.get("host"),
            "next_action": row.get("next_action", ""),
            "tags": ["burp", "web-security"] + list(tags or []),
        })
    except Exception as exc:
        continuity_warning = "Burp observation was saved, but " + _continuity_adapter_warning("canonical project continuity update", exc)
    refresh_warning = refresh_project_handoff_if_possible(pid)
    if refresh_warning:
        continuity_warning = "; ".join(filter(None, [continuity_warning, refresh_warning]))
    result = {"status": "saved", "scope": "project", "project": pid, "path": str(paths["observations"]), "observation": row}
    if continuity_warning:
        result["continuity_warning"] = continuity_warning
    return result


def burp_save_host_summary(
    project: str = "",
    hostname: str = "",
    summary: str = "",
    coverage: dict[str, Any] | None = None,
    request_refs: list[str] | None = None,
    next_action: str = "",
    source: str = "burp_mcp",
) -> dict[str, Any]:
    """Save a compact host/domain summary after direct Burp MCP live analysis."""
    pid = effective_project_for_save(project)
    wanted = normalize_hostname_filter(hostname)
    if not wanted:
        raise SystemExit("hostname is required")
    row = {
        "schema": "awoki-burp-host-summary-v1",
        "kind": "burp_host_summary",
        "created_at": now(),
        "project_related": "" if pid == "global" else pid,
        "source": source or "burp_mcp",
        "hostname": wanted,
        "summary": _brief(summary, 3000),
        "coverage": coverage or {},
        "request_refs": request_refs or [],
        "next_action": _brief(next_action, 600),
        "raw_policy": "Compact summary only. Do not place raw traffic or secrets here.",
    }
    slug = clean(wanted)
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    if pid == "global":
        out_dir = burp_root() / "host-summaries"
        ensure_dir(out_dir)
        json_path = out_dir / f"{slug}-{stamp}.json"
        md_path = out_dir / f"{slug}-{stamp}.md"
        list_path = burp_root() / "host-summaries.jsonl"
    else:
        paths = ensure_project_burp_workspace(pid)
        assert paths is not None
        out_dir = paths["host_reports"]
        json_path = out_dir / f"{slug}-{stamp}.json"
        md_path = out_dir / f"{slug}-{stamp}.md"
        list_path = paths["host_summaries"]
    write_json(json_path, row, mode=0o600)
    md = [
        f"# Burp host summary: {wanted}", "",
        f"- generated_at: {row['created_at']}",
        f"- project_related: {pid}",
        f"- source: {row['source']}",
        "", "## Summary", redact(summary or "No summary provided."),
        "", "## Coverage", "```json", json.dumps(row["coverage"], indent=2, sort_keys=True), "```",
        "", "## Request refs", *([f"- `{r}`" for r in row["request_refs"]] or ["- none recorded"]),
        "", "## Next action", row["next_action"] or "none", "",
        "Raw traffic remains in Burp or explicit evidence artifacts; this file is RAG-safe.",
    ]
    write_text(md_path, "\n".join(md) + "\n", mode=0o600)
    row.update({"json_path": str(json_path), "markdown_path": str(md_path)})
    append_jsonl(list_path, row)
    continuity_warning = ""
    if pid != "global":
        try:
            import indexing_policy
            import project_workspace
            pp = project_workspace.ensure_project_layout(awoki_root(), pid)
            relative_md = md_path.relative_to(pp.project_dir).as_posix()
            indexing_policy.register_safe_artifact(pp.index_dir, relative_md, reason="generated_redacted_burp_host_summary", source="burp")
            project_workspace.project_capture(
                awoki_root(),
                pid,
                f"Saved a Burp host summary for {wanted}.",
                kind="artifact",
                details=row["summary"],
                sources=[{"type": "file", "path": relative_md}],
                confidence="high",
                likely_continuation=row.get("next_action", ""),
                tags=["burp", "host-summary", wanted],
                metadata={"adapter": "burp", "coverage": row.get("coverage", {})},
                refresh=False,
            )
        except Exception as exc:
            continuity_warning = "Burp host summary was saved, but " + _continuity_adapter_warning("canonical project continuity update", exc)
        refresh_warning = refresh_project_handoff_if_possible(pid)
        if refresh_warning:
            continuity_warning = "; ".join(filter(None, [continuity_warning, refresh_warning]))
    result = {"status": "saved", "project": pid, "hostname": wanted, "json_path": str(json_path), "markdown_path": str(md_path), "next_action": row["next_action"]}
    if continuity_warning:
        result["continuity_warning"] = continuity_warning
    return result


def burp_task_checkpoint(
    project: str = "",
    title: str = "",
    status: str = "running",
    current_step: str = "",
    completed_steps: list[str] | None = None,
    remaining_steps: list[str] | None = None,
    next_action: str = "",
    last_tool_output_summary: str = "",
    related_refs: list[str] | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    """Write/update a checkpoint for long Burp work so 'continue' is deterministic."""
    pid = effective_project_for_save(project)
    if pid == "global":
        base = burp_root() / "tasks"
        ensure_dir(base)
        latest = burp_root() / "latest-task.txt"
    else:
        paths = ensure_project_burp_workspace(pid)
        assert paths is not None
        base = paths["tasks"]
        latest = paths["latest_task"]
    if not task_id:
        task_id = f"burp_task_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{sha12(title or next_action or str(time.time()))[:6]}"
    obj = {
        "schema": "awoki-burp-task-v1",
        "kind": "burp_task",
        "task_id": clean(task_id),
        "project_related": "" if pid == "global" else pid,
        "status": clean(status or "running"),
        "title": _brief(title or current_step or next_action, 220),
        "current_step": _brief(current_step, 800),
        "completed_steps": [_brief(x, 500) for x in (completed_steps or [])],
        "remaining_steps": [_brief(x, 500) for x in (remaining_steps or [])],
        "last_tool_output_summary": _brief(last_tool_output_summary, 1200),
        "next_action": _brief(next_action, 800),
        "related_refs": related_refs or [],
        "updated_at": now(),
        "continue_command": f"burp_task_status(project='{pid}', task_id='{clean(task_id)}') then perform next_action",
    }
    path = base / f"{clean(task_id)}.json"
    continuity_warning = ""
    write_json(path, obj, mode=0o600)
    write_text(latest, str(path) + "\n", mode=0o600)
    if pid != "global":
        try:
            import project_workspace
            pp = project_workspace.ensure_project_layout(awoki_root(), pid)
            relative_path = path.relative_to(pp.project_dir).as_posix()
            project_workspace.project_capture(
                awoki_root(),
                pid,
                f"Burp checkpoint: {obj['title']} ({obj['status']}).",
                kind="continuity_reflection",
                details=" ".join(filter(None, [obj.get("current_step", ""), obj.get("last_tool_output_summary", "")])),
                sources=[{"type": "file", "path": relative_path}] + [{"type": "burp_request_ref", "id": ref} for ref in obj.get("related_refs", [])],
                confidence="high",
                likely_continuation=obj.get("next_action", ""),
                tags=["burp", "checkpoint"],
                state=obj.get("status", ""),
                metadata={"adapter": "burp", "task_id": obj.get("task_id"), "completed_steps": obj.get("completed_steps", []), "remaining_steps": obj.get("remaining_steps", [])},
                refresh=False,
            )
        except Exception as exc:
            continuity_warning = "Burp checkpoint was saved, but " + _continuity_adapter_warning("canonical project continuity update", exc)
        refresh_warning = refresh_project_handoff_if_possible(pid)
        if refresh_warning:
            continuity_warning = "; ".join(filter(None, [continuity_warning, refresh_warning]))
    result = {"status": "checkpointed", "project": pid, "task_id": obj["task_id"], "path": str(path), "next_action": obj["next_action"], "continue_command": obj["continue_command"]}
    if continuity_warning:
        result["continuity_warning"] = continuity_warning
    return result


def burp_task_status(project: str = "", task_id: str = "", latest: bool = True) -> dict[str, Any]:
    pid = effective_project_for_save(project)
    if pid == "global":
        base = burp_root() / "tasks"
        latest_path_ = burp_root() / "latest-task.txt"
    else:
        paths = ensure_project_burp_workspace(pid)
        assert paths is not None
        base = paths["tasks"]
        latest_path_ = paths["latest_task"]
    if task_id:
        path = base / f"{clean(task_id)}.json"
    elif latest and latest_path_.exists():
        path = Path(read_text(latest_path_).strip())
    else:
        tasks = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if base.exists() else []
        if not tasks:
            return {"status": "none", "project": pid, "reason": "No Burp task checkpoint found."}
        path = tasks[0]
    if not path.exists():
        return {"status": "not_found", "project": pid, "task_id": task_id, "path": str(path)}
    obj = read_json(path)
    return {"status": "ok", "project": pid, "task": obj, "path": str(path), "next_action": obj.get("next_action", ""), "continue_command": obj.get("continue_command", "")}


def burp_task_finalize(project: str = "", task_id: str = "", outcome: str = "", finding: str = "", next_action: str = "") -> dict[str, Any]:
    st = burp_task_status(project=project, task_id=task_id, latest=not bool(task_id))
    if st.get("status") != "ok":
        return st
    task = dict(st.get("task") or {})
    pid = effective_project_for_save(project or str(task.get("project_related", "")))
    task.update({"status": "done", "outcome": _brief(outcome, 1200), "finding": _brief(finding, 1200), "next_action": _brief(next_action, 800), "finalized_at": now(), "updated_at": now()})
    path = Path(str(st.get("path")))
    write_json(path, task, mode=0o600)
    continuity_warning = ""
    if pid != "global":
        try:
            import project_workspace
            pp = project_workspace.ensure_project_layout(awoki_root(), pid)
            relative_path = path.relative_to(pp.project_dir).as_posix()
            captured = project_workspace.project_capture(
                awoki_root(),
                pid,
                finding or f"Burp task finalized: {task.get('title')}",
                kind="finding" if finding else "event",
                details=outcome,
                sources=[{"type": "file", "path": relative_path}],
                confidence="high" if finding else "medium",
                likely_continuation=next_action,
                tags=["burp", "web-security"],
                state="done",
                metadata={"adapter": "burp", "task_id": task.get("task_id")},
                refresh=False,
            )
            if finding:
                project_workspace.append_jsonl(awoki_root() / "workspace" / "projects" / pid / "memory" / "findings.jsonl", {
                    "kind": "finding", "continuity_id": captured.get("id"), "source": "burp", "title": task.get("title", "Burp task finding"), "summary": finding, "evidence": str(path), "next_action": next_action, "tags": ["burp", "web-security"]
                })
        except Exception as exc:
            continuity_warning = "Burp task was finalized, but " + _continuity_adapter_warning("canonical project continuity update", exc)
        refresh_warning = refresh_project_handoff_if_possible(pid)
        if refresh_warning:
            continuity_warning = "; ".join(filter(None, [continuity_warning, refresh_warning]))
    result = {"status": "finalized", "project": pid, "task_id": task.get("task_id"), "path": str(path), "outcome": task.get("outcome"), "next_action": task.get("next_action")}
    if continuity_warning:
        result["continuity_warning"] = continuity_warning
    return result

def validate_run(run_dir: Path) -> dict[str, Any]:
    required = ["run-manifest.json", "requests.jsonl", "endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"]
    missing = [name for name in required if not (run_dir / name).exists()]
    rows = read_jsonl(run_dir / "requests.jsonl") if (run_dir / "requests.jsonl").exists() else []
    bad_rows = [i for i, r in enumerate(rows, 1) if not isinstance(r.get("interesting_notes", []), list)]
    raw_count = len(list((run_dir / "raw").glob("*.mcp.json"))) if (run_dir / "raw").exists() else 0
    return {"status": "ok" if not missing and not bad_rows else "issues", "run_dir": str(run_dir), "missing": missing, "rows": len(rows), "raw_files": raw_count, "bad_rows": bad_rows[:20]}


def burp_records_for_rag(limit_runs: int = 25) -> list[dict[str, Any]]:
    ensure_store()
    records: list[dict[str, Any]] = []
    if not runs_dir().exists():
        return records
    for rd in sorted([p for p in runs_dir().iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True)[:limit_runs]:
        manifest = load_run_manifest(rd)
        for name in ["endpoints.md", "auth-cookies.md", "variables.md", "interesting.md", "handoff.md"]:
            p = rd / name
            if not p.exists():
                continue
            text = redact(read_text(p))
            records.append({
                "scope": "global",
                "kind": "burp_inventory",
                "title": f"Burp {manifest.get('source_type', '')} {rd.name} {name}",
                "text": text[:12000],
                "run_id": rd.name,
                "source_type": manifest.get("source_type", ""),
                "target": manifest.get("target", ""),
                "project_related": manifest.get("project_related", ""),
                "tags": manifest.get("tags", []) + ["burp", "http", "security-testing"],
                "_source_file": str(p),
            })
    return records


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Awoki Burp script integration: MCP-backed pulls to disk, compact redacted inventories to RAG.")
    p.add_argument("--target", default=DEFAULT_TARGET)
    p.add_argument("--project-related", default="")
    p.add_argument("--tags", default="")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-profile").set_defaults(func=lambda a: print_json(ensure_store()))
    sub.add_parser("status").set_defaults(func=lambda a: print_json(status()))
    x = sub.add_parser("tools"); x.add_argument("--save", action="store_true"); x.set_defaults(func=lambda a: print_json(mcp_tools(a.target, burp_root() if a.save else None)))
    x = sub.add_parser("start"); x.add_argument("--source", default="manual"); x.add_argument("--project-related", default=""); x.add_argument("--tags", default=""); x.set_defaults(func=lambda a: print_json({"status": "created", "run_dir": str(create_run(a.source, a.target, a.project_related, split_tags(a.tags))) }))
    for cmd, source in [("pull-history", "history"), ("pull-history-regex", "history_regex"), ("pull-websocket-history", "websocket_history"), ("pull-organizer", "organizer"), ("pull-active-editor", "active_editor"), ("pull-repeater", "repeater"), ("pull-intruder", "intruder")]:
        x = sub.add_parser(cmd)
        x.add_argument("--count", type=int, default=50)
        x.add_argument("--offset", type=int, default=0)
        x.add_argument("--pages", type=int, default=1)
        x.add_argument("--regex", default="")
        x.add_argument("--run-dir", default="")
        x.add_argument("--project-related", default="")
        x.add_argument("--tags", default="")
        x.set_defaults(func=lambda a, s=source: print_json(pull_source(s, target=a.target, count=a.count, offset=a.offset, pages=a.pages, regex=a.regex, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    def add_request_builder_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--method", default="GET")
        parser.add_argument("--url", default="")
        parser.add_argument("--header", action="append", default=[])
        parser.add_argument("--body", default="")
        parser.add_argument("--body-file", default="")
        parser.add_argument("--raw", default="")
        parser.add_argument("--raw-file", default="")
        parser.add_argument("--target-hostname", default="")
        parser.add_argument("--target-port", type=int)
        proto = parser.add_mutually_exclusive_group()
        proto.add_argument("--https", action="store_true")
        proto.add_argument("--http", action="store_true")
        parser.add_argument("--run-dir", default="")
        parser.add_argument("--project-related", default="")
        parser.add_argument("--tags", default="")

    x = sub.add_parser("send-request")
    add_request_builder_args(x)
    x.set_defaults(func=lambda a: print_json(send_request(a.method, a.url, headers=parse_header_options(a.header), body=body_from_args(a.body, a.body_file), target=a.target, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("send-raw-request")
    add_request_builder_args(x)
    x.set_defaults(func=lambda a: print_json(send_raw_http1(raw_request_from_cli_args(a), target=a.target, target_hostname=a.target_hostname, target_port=a.target_port, uses_https=True if a.https else (False if a.http else None), url=a.url, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("create-repeater-tab")
    add_request_builder_args(x)
    x.add_argument("--tab-name", default="")
    x.set_defaults(func=lambda a: print_json(create_repeater_from_raw(raw_request_from_cli_args(a), target=a.target, target_hostname=a.target_hostname, target_port=a.target_port, uses_https=True if a.https else (False if a.http else None), url=a.url, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("send-to-intruder")
    add_request_builder_args(x)
    x.add_argument("--tab-name", default="")
    x.set_defaults(func=lambda a: print_json(send_to_intruder_from_raw(raw_request_from_cli_args(a), target=a.target, target_hostname=a.target_hostname, target_port=a.target_port, uses_https=True if a.https else (False if a.http else None), url=a.url, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("set-active-editor")
    x.add_argument("--text", default="")
    x.add_argument("--raw", default="")
    x.add_argument("--raw-file", default="")
    x.set_defaults(func=lambda a: print_json(set_active_editor_text(read_text(Path(a.raw_file)) if a.raw_file else (a.raw or a.text), target=a.target)))

    x = sub.add_parser("active-to-repeater")
    x.add_argument("--tab-name", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--tags", default="")
    x.set_defaults(func=lambda a: print_json(active_to_repeater(target=a.target, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("active-to-intruder")
    x.add_argument("--tab-name", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--tags", default="")
    x.set_defaults(func=lambda a: print_json(active_to_intruder(target=a.target, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags), run_dir=a.run_dir)))

    x = sub.add_parser("history-to-repeater")
    x.add_argument("--burp-id", required=True)
    x.add_argument("--source-type", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--tab-name", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--tags", default="")
    x.set_defaults(func=lambda a: print_json(history_to_repeater(a.burp_id, target=a.target, run_dir=a.run_dir, source_type=a.source_type, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags))))

    x = sub.add_parser("history-to-intruder")
    x.add_argument("--burp-id", required=True)
    x.add_argument("--source-type", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--tab-name", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--tags", default="")
    x.set_defaults(func=lambda a: print_json(history_to_intruder(a.burp_id, target=a.target, run_dir=a.run_dir, source_type=a.source_type, tab_name=a.tab_name, project_related=a.project_related, tags=split_tags(a.tags))))

    x = sub.add_parser("run-list")
    x.add_argument("--project-related", default="")
    x.add_argument("--limit", type=int, default=0)
    x.add_argument("--all", action="store_true")
    x.set_defaults(func=lambda a: print_json(burp_run_list(project_related=a.project_related, limit=a.limit, all_runs=a.all)))

    x = sub.add_parser("run-summary")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--preview", type=int, default=10)
    x.set_defaults(func=lambda a: print_json(burp_run_summary(run_id_value=a.run_id, run_dir=a.run_dir, preview=a.preview)))

    x = sub.add_parser("find-request")
    x.add_argument("--pattern", required=True)
    x.add_argument("--project-related", default="")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--all", action="store_true")
    x.add_argument("--limit-runs", type=int, default=0)
    x.add_argument("--max-matches", type=int, default=10)
    x.set_defaults(func=lambda a: print_json(burp_find_request(pattern=a.pattern, project_related=a.project_related, run_dir=a.run_dir, run_id_value=a.run_id, all_runs=a.all, limit_runs=a.limit_runs, max_matches=a.max_matches)))

    x = sub.add_parser("show-request")
    x.add_argument("--request-ref", default="")
    x.add_argument("--pattern", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--all", action="store_true")
    x.add_argument("--limit-runs", type=int, default=0)
    x.add_argument("--max-bytes", type=int, default=6000)
    x.set_defaults(func=lambda a: print_json(burp_show_request(request_ref_value=a.request_ref, pattern=a.pattern, project_related=a.project_related, run_dir=a.run_dir, run_id_value=a.run_id, all_runs=a.all, limit_runs=a.limit_runs, max_bytes=a.max_bytes)))

    x = sub.add_parser("request-to-repeater")
    x.add_argument("--request-ref", default="")
    x.add_argument("--pattern", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--all", action="store_true")
    x.add_argument("--limit-runs", type=int, default=0)
    x.add_argument("--tab-name", default="")
    x.set_defaults(func=lambda a: print_json(burp_request_to_repeater(request_ref_value=a.request_ref, pattern=a.pattern, target=a.target, tab_name=a.tab_name, project_related=a.project_related, run_dir=a.run_dir, run_id_value=a.run_id, all_runs=a.all, limit_runs=a.limit_runs)))

    x = sub.add_parser("request-to-intruder")
    x.add_argument("--request-ref", default="")
    x.add_argument("--pattern", default="")
    x.add_argument("--project-related", default="")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--all", action="store_true")
    x.add_argument("--limit-runs", type=int, default=0)
    x.add_argument("--tab-name", default="")
    x.set_defaults(func=lambda a: print_json(burp_request_to_intruder(request_ref_value=a.request_ref, pattern=a.pattern, target=a.target, tab_name=a.tab_name, project_related=a.project_related, run_dir=a.run_dir, run_id_value=a.run_id, all_runs=a.all, limit_runs=a.limit_runs)))

    x = sub.add_parser("extract-request")
    x.add_argument("--pattern", default="")
    x.add_argument("--burp-id", default="")
    x.add_argument("--request-ref", default="")
    x.add_argument("--source-type", default="")
    x.add_argument("--run-id", default="")
    x.add_argument("--run-dir", default="")
    x.add_argument("--all", action="store_true")
    x.add_argument("--limit-runs", type=int, default=0)
    x.add_argument("--output", default="")
    x.add_argument("--name", default="")
    x.add_argument("--project-related", default="")
    x.set_defaults(func=lambda a: print_json(extract_request(pattern=a.pattern, burp_id=a.burp_id, request_ref_value=a.request_ref, run_dir=a.run_dir, run_id_value=a.run_id, source_type=a.source_type, output=a.output, name=a.name, project_related=a.project_related, all_runs=a.all, limit_runs=a.limit_runs)))

    x = sub.add_parser("host-report")
    x.add_argument("--hostname", required=True)
    x.add_argument("--project-related", default="")
    x.add_argument("--include-global", action="store_true", default=True)
    x.add_argument("--no-include-global", dest="include_global", action="store_false")
    x.add_argument("--all-projects", action="store_true")
    x.add_argument("--run-limit", type=int, default=0)
    x.add_argument("--max-items", type=int, default=100)
    x.add_argument("--no-write-artifacts", dest="write_artifacts", action="store_false", default=True)
    x.set_defaults(func=lambda a: print_json(burp_host_report(hostname=a.hostname, project_related=a.project_related, include_global=a.include_global, all_projects=a.all_projects, run_limit=a.run_limit, max_items=a.max_items, write_artifacts=a.write_artifacts)))


    x = sub.add_parser("record-observation")
    x.add_argument("--project", default="")
    x.add_argument("--title", default="")
    x.add_argument("--summary", default="")
    x.add_argument("--host", default="")
    x.add_argument("--method", default="")
    x.add_argument("--path", default="")
    x.add_argument("--status-code", default="")
    x.add_argument("--request-ref", default="")
    x.add_argument("--artifact", default="")
    x.add_argument("--next-action", default="")
    x.add_argument("--source", default="burp_mcp")
    x.add_argument("--tags", default="")
    x.set_defaults(func=lambda a: print_json(burp_record_observation(project=a.project or a.project_related, title=a.title, summary=a.summary, host=a.host, method=a.method, path=a.path, status_code=a.status_code, request_ref_value=a.request_ref, artifact=a.artifact, next_action=a.next_action, source=a.source, tags=split_tags(a.tags))))

    x = sub.add_parser("save-host-summary")
    x.add_argument("--project", default="")
    x.add_argument("--hostname", required=True)
    x.add_argument("--summary", required=True)
    x.add_argument("--coverage-json", default="{}")
    x.add_argument("--request-ref", action="append", default=[])
    x.add_argument("--next-action", default="")
    x.add_argument("--source", default="burp_mcp")
    def _save_host_summary_cli(a):
        try:
            coverage = json.loads(a.coverage_json or "{}")
            if not isinstance(coverage, dict):
                coverage = {"value": coverage}
        except Exception as exc:
            coverage = {"parse_error": str(exc), "raw": a.coverage_json}
        print_json(burp_save_host_summary(project=a.project or a.project_related, hostname=a.hostname, summary=a.summary, coverage=coverage, request_refs=a.request_ref, next_action=a.next_action, source=a.source))
    x.set_defaults(func=_save_host_summary_cli)

    x = sub.add_parser("task-checkpoint")
    x.add_argument("--project", default="")
    x.add_argument("--task-id", default="")
    x.add_argument("--title", default="")
    x.add_argument("--status", default="running")
    x.add_argument("--current-step", default="")
    x.add_argument("--completed-step", action="append", default=[])
    x.add_argument("--remaining-step", action="append", default=[])
    x.add_argument("--next-action", default="")
    x.add_argument("--last-tool-output-summary", default="")
    x.add_argument("--related-ref", action="append", default=[])
    x.set_defaults(func=lambda a: print_json(burp_task_checkpoint(project=a.project or a.project_related, title=a.title, status=a.status, current_step=a.current_step, completed_steps=a.completed_step, remaining_steps=a.remaining_step, next_action=a.next_action, last_tool_output_summary=a.last_tool_output_summary, related_refs=a.related_ref, task_id=a.task_id)))

    x = sub.add_parser("task-status")
    x.add_argument("--project", default="")
    x.add_argument("--task-id", default="")
    x.add_argument("--latest", action="store_true", default=True)
    x.set_defaults(func=lambda a: print_json(burp_task_status(project=a.project or a.project_related, task_id=a.task_id, latest=a.latest)))

    x = sub.add_parser("task-finalize")
    x.add_argument("--project", default="")
    x.add_argument("--task-id", default="")
    x.add_argument("--outcome", default="")
    x.add_argument("--finding", default="")
    x.add_argument("--next-action", default="")
    x.set_defaults(func=lambda a: print_json(burp_task_finalize(project=a.project or a.project_related, task_id=a.task_id, outcome=a.outcome, finding=a.finding, next_action=a.next_action)))

    x = sub.add_parser("inventory"); x.add_argument("run_dir", nargs="?"); x.set_defaults(func=lambda a: print_json(rebuild(Path(a.run_dir) if a.run_dir else latest_run_dir())))
    x = sub.add_parser("validate"); x.add_argument("run_dir", nargs="?"); x.set_defaults(func=lambda a: print_json(validate_run(Path(a.run_dir) if a.run_dir else latest_run_dir())))
    sub.add_parser("latest").set_defaults(func=lambda a: print(str(latest_run_dir())))
    return p


def split_tags(value: str) -> list[str]:
    return [t.strip() for t in str(value or "").split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
