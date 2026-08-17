#!/usr/bin/env python3
from __future__ import annotations

import json
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
    tsc = shutil.which("tsc")
    if not tsc:
        print("TypeScript compiler not available; skipping OpenCode plugin validation")
        return 0
    if not PLUGIN.exists():
        raise SystemExit(f"missing OpenCode plugin: {PLUGIN}")
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
        subprocess.run(
            [
                tsc,
                "--target", "ES2022",
                "--module", "commonjs",
                "--moduleResolution", "node",
                "--skipLibCheck",
                "--noImplicitAny", "false",
                "--lib", "ES2022,DOM",
                "--outDir", str(output_dir),
                str(stub_path),
                str(PLUGIN),
            ],
            cwd=ROOT,
            check=True,
        )
        compiled = output_dir / "awoki-continuity.js"
        if not compiled.is_file():
            raise SystemExit(f"TypeScript compiler did not produce {compiled}")

        node = shutil.which("node")
        if node:
            script = f'''
const plugin = require({json.dumps(str(compiled))});
(async () => {{
  const hooks = await plugin.AwokiContinuity({{
    client: {{ app: {{ log: async () => {{}} }} }},
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
}})().catch((error) => {{
  console.error(error);
  process.exit(1);
}});
'''
            subprocess.run([node, "--eval", script], cwd=ROOT, check=True)
            print("OpenCode continuity plugin compile and session-hook runtime check ok")
        else:
            print("OpenCode continuity plugin static compile ok; Node unavailable for hook runtime check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
