"""Offline request-contract checks: loopback only, synthetic data and keys only.

Embeddings/reranking use Awoki's real runtime clients. Chat uses the compiled
OpenCode provider options with a small HTTP probe, not an OpenCode agent run.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def check_contract(mode: str, header: str) -> list[dict]:
    import httpx
    import rag_backend

    captured = []

    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, self.headers, body))
            if self.path == "/v1/embeddings":
                payload = {"data": [{"index": 0, "embedding": [1.0] + [0.0] * 63}], "model": body["model"], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
            elif self.path == "/rerank":
                payload = {"results": [{"index": 0, "relevance_score": 0.9}]}
            elif self.path == "/v1/chat/completions":
                payload = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
            else:
                self.send_error(404)
                return
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="awoki-provider-contract-") as temporary:
            root = Path(temporary)
            script = ROOT / ".harness/bin/awoki-ai-configure"
            compiler = runpy.run_path(str(script))
            env = {"AWOKI_ROOT": str(root), "AWOKI_AI_API_KEY": "synthetic-contract-token"}
            args = [str(script), "--non-interactive", "--base-url", f"http://127.0.0.1:{server.server_port}/v1", "--auth-mode", mode, "--auth-header", header, "--vector-size", "64"]
            with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", args), redirect_stdout(io.StringIO()):
                compiler["main"]()
            _, runtime = compiler["_read_env"](root / ".env")
            config = json.loads((root / ".opencode-state/config/opencode.jsonc").read_text())
            with patch.dict(os.environ, runtime, clear=True):
                rag_backend.embed_texts(["synthetic document"])
                hits = rag_backend.rerank_hits("synthetic query", [{"preview": "synthetic document"}])
                if not hits or hits[0].get("rerank_fallback"):
                    raise RuntimeError("reranker contract failed")
                options = config["provider"]["custom-openai"]["options"]
                headers = {key: value.replace("{env:AWOKI_EMBEDDING_API_KEY}", runtime["AWOKI_EMBEDDING_API_KEY"]) for key, value in options["headers"].items()}
                response = httpx.post(options["baseURL"] + "/chat/completions", headers=headers, json={"model": runtime["AWOKI_OPENCODE_MODEL"], "messages": [{"role": "user", "content": "synthetic request"}]}, timeout=5, trust_env=False)
                response.raise_for_status()
            expected_header = "Authorization" if mode == "bearer" else header
            expected_value = ("Bearer " if mode == "bearer" else "") + "synthetic-contract-token"
            expected = [
                ("/v1/embeddings", runtime["AWOKI_EMBEDDING_MODEL"], "input", "runtime-client"),
                ("/rerank", runtime["AWOKI_RERANK_MODEL"], "documents", "runtime-client"),
                ("/v1/chat/completions", runtime["AWOKI_OPENCODE_MODEL"], "messages", "config-derived-http"),
            ]
            if len(captured) != len(expected):
                raise RuntimeError("expected exactly three local provider requests")
            results = []
            for (path, headers, body), (expected_path, model, field, transport) in zip(captured, expected):
                if path != expected_path or body.get("model") != model or not body.get(field):
                    raise RuntimeError("provider request path/model/body contract failed")
                if headers.get_all(expected_header) != [expected_value]:
                    raise RuntimeError("provider authentication header contract failed")
                if expected_header.lower() != "authorization" and headers.get_all("authorization"):
                    raise RuntimeError("unexpected Authorization header in custom-header mode")
                results.append({"path": path, "model": model, "auth_mode": mode, "header": expected_header, "transport": transport, "status": "ok"})
            return results
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def main() -> int:
    for mode, header in (("bearer", "Authorization"), ("header", "Authorization"), ("header", "authorization"), ("header", "X-API-Key")):
        for result in check_contract(mode, header):
            print(json.dumps(result))
    print("awoki_provider_contracts=ok (offline structure only; no real key validation or OpenCode agent run)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ImportError:
        raise SystemExit("Install Awoki requirements in your development environment, or run this check in the Awoki image.")
