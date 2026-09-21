#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".opencode" / "plugins" / "awoki-continuity.ts"
CODE_SESSION_TOOLS = [
    "codebase_search",
    "code_index_status",
    "code_index_verify",
    "code_definition",
    "code_callers",
    "code_callees",
    "code_path",
    "code_flow_graph",
    "code_source_window",
    "code_text_search",
    "code_validate_claim",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-sdk", action="store_true", help="fail unless real matching SDK types are checked")
    args = parser.parse_args()
    modules = Path(os.environ.get("AWOKI_PLUGIN_NODE_MODULES") or ROOT / ".opencode" / "node_modules")
    sdk_types = modules / "@opencode-ai" / "plugin" / "dist" / "index.d.ts"
    real_sdk = sdk_types.is_file()
    tsc = shutil.which("tsc") or (str(modules / ".bin" / "tsc") if (modules / ".bin" / "tsc").exists() else None)
    if args.require_sdk and (not tsc or not real_sdk):
        raise SystemExit("real SDK validation requires TypeScript and matching @opencode-ai/plugin + SDK packages")
    if not tsc:
        print("TypeScript compiler not available; skipping OpenCode plugin validation")
        return 0
    if not PLUGIN.exists():
        raise SystemExit(f"missing OpenCode plugin: {PLUGIN}")
    if real_sdk:
        plugin_version = json.loads((modules / "@opencode-ai/plugin/package.json").read_text())["version"]
        sdk_version = json.loads((modules / "@opencode-ai/sdk/package.json").read_text())["version"]
        if plugin_version != sdk_version:
            raise SystemExit("OpenCode plugin and SDK versions must match for compatibility validation")
        stub = 'declare const Bun: any\n'
    else:
        # This portable smoke test is not an SDK compatibility verdict. CI uses
        # --require-sdk and may never take this branch.
        stub = '''declare module "@opencode-ai/plugin" {
  export type Plugin = (ctx: any) => Promise<Record<string, any>>
}
declare module "node:path" {
  export function join(...parts: string[]): string
}
declare const Bun: any
'''
    with tempfile.TemporaryDirectory(prefix="awoki-tsc-") as td:
        temp = Path(td)
        stub_path = temp / "opencode-plugin-stub.d.ts"
        output_dir = temp / "compiled"
        stub_path.write_text(stub, encoding="utf-8")
        config = {
            "compilerOptions": {
                "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
                "skipLibCheck": True, "noImplicitAny": False, "lib": ["ES2022", "DOM"],
                "outDir": str(output_dir), "rootDir": str(PLUGIN.parent),
            },
            "files": [str(stub_path), str(PLUGIN)],
        }
        if real_sdk:
            config["compilerOptions"].update({
                "paths": {"@opencode-ai/plugin": [str(sdk_types)]},
                "typeRoots": [str(modules / "@types")],
                "types": ["node"],
            })
        config_path = temp / "tsconfig.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        subprocess.run([tsc, "--project", str(config_path)], cwd=ROOT, check=True)
        if real_sdk:
            print(f"OpenCode plugin checked against actual SDK {sdk_version}")
        else:
            print("OpenCode plugin stub-only smoke check; SDK compatibility NOT verified")
        compiled = output_dir / "awoki-continuity.js"
        if not compiled.is_file():
            raise SystemExit(f"TypeScript compiler did not produce {compiled}")
        (output_dir / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")

        node = shutil.which("node")
        if node:
            script = f'''
const plugin = await import({json.dumps(compiled.as_uri())});
const bridgeCalls = [];
globalThis.Bun = {{ spawn: (args) => {{
  bridgeCalls.push(args);
  return {{stdout: new Response("{{}}").body, stderr: new Response("").body, exited: Promise.resolve(0)}};
}} }};
(async () => {{
  const hooks = await plugin.AwokiContinuity({{
    client: {{ app: {{ log: async () => {{}} }}, session: {{ message: async (request) => {{
      if (request.path.id !== "session-turn-test" || request.path.messageID !== "u1") throw new Error("unexpected initial-user lookup");
      return {{data: {{info: {{id: "u1", sessionID: "session-turn-test", role: "user"}},
        parts: [{{type: "text", text: "fixture user", sessionID: "session-turn-test", messageID: "u1"}}]}}}};
    }} }} }},
    directory: process.cwd(),
  }});
  const tools = {json.dumps(CODE_SESSION_TOOLS)};
  for (const tool of tools) {{
    for (const rawName of [tool, `mcp_awoki_${{tool}}`]) {{
      const output = {{ args: {{}} }};
      await hooks["tool.execute.before"](
        {{ tool: rawName, sessionID: "session-runtime-check" }},
        output,
      );
      if (output.args.session_id !== "session-runtime-check") {{
        throw new Error(`${{rawName}} did not receive the active OpenCode session`);
      }}
    }}
    const preserved = {{ args: {{ session_id: "explicit-session" }} }};
    await hooks["tool.execute.before"](
      {{ tool, sessionID: "session-runtime-check" }},
      preserved,
    );
    if (preserved.args.session_id !== "explicit-session") {{
      throw new Error(`${{tool}} overwrote an explicit session_id`);
    }}
  }}
  const emit = async (type, properties) => hooks.event({{event: {{type, properties}}}});
  const sid = "session-turn-test";
  await emit("message.updated", {{info: {{id: "u1", sessionID: sid, role: "user"}}}});
  await emit("message.updated", {{info: {{id: "a1", sessionID: sid, role: "assistant", parentID: "u1", finish: "tool-calls"}}}});
  await emit("message.part.updated", {{part: {{messageID: "a1", sessionID: sid, type: "text", text: "   "}}}});
  await emit("message.updated", {{info: {{id: "u1", sessionID: sid, role: "user"}}}});
  await emit("session.idle", {{sessionID: sid}});
  const terminal = bridgeCalls.filter(a => a.includes("agent-turn-terminal")).at(-1);
  if (!terminal || terminal[terminal.indexOf("--parent-message-id") + 1] !== "u1" || terminal.includes("--has-text")) {{
    throw new Error("current-turn parent attribution or empty-text boundary failed");
  }}
  await emit("message.updated", {{info: {{id: "s1", sessionID: sid, role: "assistant", parentID: "u1", summary: true, finish: "stop"}}}});
  await emit("session.idle", {{sessionID: sid}});
  if (!bridgeCalls.filter(a => a.includes("agent-turn-terminal")).at(-1).includes("--is-summary")) {{
    throw new Error("compaction summary was not distinguished from an answer");
  }}
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
'''
            subprocess.run([node, "--input-type=module", "--eval", script], cwd=ROOT, check=True)
            print("OpenCode continuity plugin compile and session-hook runtime check ok")
        else:
            print("OpenCode continuity plugin static compile ok; Node unavailable for hook runtime check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
