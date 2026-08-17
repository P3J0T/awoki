import type { Plugin } from "@opencode-ai/plugin"
// OpenCode compatibility is resolved at image build: CLI, @opencode-ai/plugin, and @opencode-ai/sdk are materialized at one version and runtime-checked.
import { join } from "node:path"

export const AwokiContinuity: Plugin = async ({ client, directory }) => {
  const bridge = join(directory, ".harness", "opencode_events.py")
  const sessionAwareTools = new Set([
    "project_open",
    "project_repo_add",
    "project_repo_list",
    "project_repo_remove",
    "project_repo_default",
    "project_source_add",
    "project_source_list",
    "project_source_remove",
    "project_source_default",
    "project_create",
    "project_resume",
    "project_capture",
    "project_continuation_schedule",
    "project_continuation_status",
    "project_continuation_cancel",
    "project_continuation_finalize",
    "project_task_finalize",
    "project_task_status",
    "project_task_checkpoint",
    "session_work_status",
    "session_runtime_status",
    "reference_describe",
    "reference_annotate",
    "reference_resolve",
    "acceptance_run_start",
    "acceptance_run_status",
    "acceptance_run_next",
    "acceptance_evidence_get",
    "acceptance_run_record",
    "acceptance_run_record_invariant",
    "acceptance_run_finalize",
    "harness_self_check",
    "project_search",
    "codebase_search",
    "code_diagnostics_trace",
    "code_index_status",
    "code_index_verify",
    "code_definition",
    "code_callers",
    "code_callees",
    "code_path",
    "code_flow_graph",
    "code_source_window",
    "code_evidence_verify",
    "code_semantics_check",
    "code_exact_search",
    "code_text_search",
    "code_validate_claim",
    "project_refresh",
    "code_index_refresh_start",
    "code_index_refresh_status",
    "code_index_refresh_cancel",
    "code_vector_refresh_start",
    "code_vector_refresh_status",
    "code_vector_refresh_cancel",
    "repository_prepare_start",
    "repository_prepare_status",
    "repository_prepare_cancel",
    "project_pause",
    "project_status",
    "project_index_preview",
    "harness_status",
    "index_project",
    "search_rag",
    "search_project_memory",
    "save_project_fact",
    "save_finding",
    "save_hypothesis",
    "reliability_start",
    "reliability_record_check",
    "reliability_record_claim",
    "reliability_record_assessment",
    "reliability_record_relation",
    "reliability_consume_corrective_budget",
    "reliability_aggregate_verdict",
    "reliability_verification_checkpoint",
    "reliability_verify_code_claim",
    "reliability_verify_semantics_claim",
    "reliability_finish",
    "reliability_status",
  ])
  const projectOpenTools = new Set(["project_open", "project_create", "project_resume"])
  const continuationTools = new Set([
    "project_continuation_schedule",
    "project_continuation_status",
    "project_continuation_cancel",
    "project_continuation_finalize",
  ])
  const continuityMaintenanceTools = new Set([
    "project_open",
    "project_repo_add",
    "project_repo_list",
    "project_repo_remove",
    "project_repo_default",
    "project_source_add",
    "project_source_list",
    "project_source_remove",
    "project_source_default",
    "project_create",
    "project_resume",
    "project_capture",
    ...continuationTools,
    "project_task_finalize",
    "project_task_status",
    "project_task_checkpoint",
    "session_work_status",
    "session_runtime_status",
    "reference_describe",
    "reference_annotate",
    "reference_resolve",
    "acceptance_run_start",
    "acceptance_run_status",
    "acceptance_run_next",
    "acceptance_evidence_get",
    "acceptance_run_record",
    "acceptance_run_record_invariant",
    "acceptance_run_finalize",
    "harness_self_check",
    "project_search",
    "codebase_search",
    "code_diagnostics_trace",
    "code_index_status",
    "code_index_verify",
    "code_definition",
    "code_callers",
    "code_callees",
    "code_path",
    "code_flow_graph",
    "code_source_window",
    "code_evidence_verify",
    "code_semantics_check",
    "code_exact_search",
    "code_text_search",
    "code_validate_claim",
    "project_refresh",
    "code_index_refresh_start",
    "code_index_refresh_status",
    "code_index_refresh_cancel",
    "code_vector_refresh_start",
    "code_vector_refresh_status",
    "code_vector_refresh_cancel",
    "repository_prepare_start",
    "repository_prepare_status",
    "repository_prepare_cancel",
    "project_pause",
    "project_status",
    "project_index_preview",
    "save_project_fact",
    "save_finding",
    "save_hypothesis",
    "reliability_start",
    "reliability_record_check",
    "reliability_record_claim",
    "reliability_record_assessment",
    "reliability_record_relation",
    "reliability_consume_corrective_budget",
    "reliability_aggregate_verdict",
    "reliability_verification_checkpoint",
    "reliability_verify_code_claim",
    "reliability_verify_semantics_claim",
    "reliability_finish",
    "reliability_status",
  ])

  const timers = new Map<string, ReturnType<typeof setTimeout>>()
  const idleSessions = new Set<string>()
  type AssistantTurnState = {
    messageID: string; finish: string; hasReasoning: boolean; hasText: boolean; hasTool: boolean;
    providerID: string; modelID: string; agentMode: string; errorType: string;
    stepFinishSeen: boolean; inputTokens: number; outputTokens: number; reasoningTokens: number;
    toolExecutionsCompleted: number;
  }
  const assistantTurns = new Map<string, AssistantTurnState>()
  const latestAssistantBySession = new Map<string, string>()
  const nativeToolNames = new Set(["bash", "read", "write", "edit", "patch", "glob", "grep", "list", "task", "todowrite"])
  const acceptanceSessions = new Set<string>()
  const acceptanceObservableOrchestrationTools = new Set([
    "acceptance_run_start", "acceptance_run_status", "acceptance_run_next",
  ])
  const acceptanceControlTools = new Set([
    "acceptance_run_record", "acceptance_run_record_invariant", "acceptance_run_finalize",
  ])

  const log = async (level: "debug" | "info" | "warn" | "error", message: string, extra: Record<string, unknown> = {}) => {
    try {
      await client.app.log({ body: { service: "awoki-continuity", level, message, extra } })
    } catch {
      // Continuity must never make OpenCode fail because logging is unavailable.
    }
  }

  const runBridge = async (args: string[], payload?: Record<string, unknown>): Promise<Record<string, any>> => {
    try {
      const proc = Bun.spawn(["python3", bridge, ...args], {
        cwd: directory,
        stdin: payload ? new Blob([JSON.stringify(payload)]) : "ignore",
        stdout: "pipe",
        stderr: "pipe",
      })
      const [stdout, stderr, exitCode] = await Promise.all([
        new Response(proc.stdout).text(),
        new Response(proc.stderr).text(),
        proc.exited,
      ])
      if (exitCode !== 0) {
        await log("warn", "Awoki event bridge failed", { exitCode, stderr: stderr.slice(0, 2_000) })
        return {}
      }
      return stdout.trim() ? JSON.parse(stdout) : {}
    } catch (error) {
      await log("warn", "Awoki event bridge raised an error", { error: String(error) })
      return {}
    }
  }

  const findString = (value: unknown, keys: Set<string>, depth = 0): string => {
    if (depth > 4 || value === null || value === undefined) return ""
    if (typeof value === "object") {
      for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
        if (keys.has(key) && typeof nested === "string" && nested.trim()) return nested
      }
      for (const nested of Object.values(value as Record<string, unknown>)) {
        const found = findString(nested, keys, depth + 1)
        if (found) return found
      }
    }
    return ""
  }

  const sessionID = (value: unknown): string =>
    findString(value, new Set(["sessionID", "sessionId", "session_id"]))

  const filePath = (value: unknown): string =>
    findString(value, new Set(["path", "filePath", "filepath", "file"]))

  const messageInfo = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== "object") return {}
    const info = (value as any)?.properties?.info ?? (value as any)?.info
    return info && typeof info === "object" ? info as Record<string, unknown> : {}
  }

  const messageRole = (value: unknown): string => {
    const role = messageInfo(value).role
    return (typeof role === "string" ? role : findString(value, new Set(["role"]))).toLowerCase()
  }

  const messageID = (value: unknown): string => {
    const id = messageInfo(value).id
    return typeof id === "string" ? id : findString(value, new Set(["messageID", "messageId", "message_id"]))
  }

  const messageFinish = (value: unknown): string => {
    const info = messageInfo(value) as any
    const raw = info.finish ?? info.finishReason ?? info.finish_reason ?? ""
    return typeof raw === "string" ? raw.toLowerCase() : ""
  }

  const eventPart = (value: unknown): Record<string, unknown> => {
    if (!value || typeof value !== "object") return {}
    const part = (value as any)?.properties?.part ?? (value as any)?.part
    return part && typeof part === "object" ? part as Record<string, unknown> : {}
  }

  const partMessageID = (value: unknown): string => {
    const part = eventPart(value) as any
    const raw = part.messageID ?? part.messageId ?? part.message_id ?? (value as any)?.properties?.messageID ?? ""
    return typeof raw === "string" ? raw : ""
  }

  const updatePartState = (value: unknown) => {
    const mid = partMessageID(value)
    if (!mid) return
    const current = assistantTurns.get(mid) ?? { messageID: mid, finish: "", hasReasoning: false, hasText: false, hasTool: false, providerID: "", modelID: "", agentMode: "", errorType: "", stepFinishSeen: false, inputTokens: 0, outputTokens: 0, reasoningTokens: 0, toolExecutionsCompleted: 0 }
    const part = eventPart(value) as any
    const type = String(part.type ?? "").toLowerCase()
    if (type === "reasoning" || type.includes("reasoning")) current.hasReasoning = true
    else if (type === "text") current.hasText = true
    else if (type === "tool" || type.includes("tool")) current.hasTool = true
    else if (type === "step-finish") {
      current.stepFinishSeen = true
      current.finish = String(part.reason ?? current.finish ?? "").toLowerCase()
      const tokens = part.tokens && typeof part.tokens === "object" ? part.tokens : {}
      current.inputTokens = Number.isFinite(Number(tokens.input)) ? Math.max(0, Number(tokens.input)) : current.inputTokens
      current.outputTokens = Number.isFinite(Number(tokens.output)) ? Math.max(0, Number(tokens.output)) : current.outputTokens
      current.reasoningTokens = Number.isFinite(Number(tokens.reasoning)) ? Math.max(0, Number(tokens.reasoning)) : current.reasoningTokens
    }
    assistantTurns.set(mid, current)
  }

  const recordTerminalTurn = async (sid: string) => {
    const mid = latestAssistantBySession.get(sid) || ""
    const state = mid ? assistantTurns.get(mid) : undefined
    if (!state) return
    const args = [
      "agent-turn-terminal", "--session-id", sid, "--message-id", state.messageID,
      "--finish-reason", state.finish || "",
      "--provider-id", state.providerID || "", "--model-id", state.modelID || "", "--agent-mode", state.agentMode || "",
      "--error-type", state.errorType || "",
      "--input-tokens", String(state.inputTokens || 0), "--output-tokens", String(state.outputTokens || 0),
      "--reasoning-tokens", String(state.reasoningTokens || 0),
      "--tool-executions-completed", String(state.toolExecutionsCompleted || 0),
    ]
    if (state.stepFinishSeen) args.push("--step-finish-seen")
    if (state.hasReasoning) args.push("--has-reasoning")
    if (state.hasText) args.push("--has-text")
    if (state.hasTool) args.push("--has-tool")
    const result = await runBridge(args)
    if (result.runtime_state === "degraded") {
      await log("warn", "Awoki detected terminal assistant-turn anomaly", {
        sessionID: sid, messageID: state.messageID, finishReason: state.finish,
        reasoningPresent: state.hasReasoning, textPresent: state.hasText, toolPresent: state.hasTool,
        classification: result?.last_anomaly?.classification ?? "unknown",
      })
    }
  }

  const eventTodos = (value: unknown): Array<Record<string, unknown>> => {
    if (!value || typeof value !== "object") return []
    const direct = (value as any)?.properties?.todos ?? (value as any)?.todos
    if (!Array.isArray(direct)) return []
    return direct
      .filter((row) => row && typeof row === "object")
      .slice(0, 64)
      .map((row: any) => ({
        id: typeof row.id === "string" ? row.id.slice(0, 200) : "",
        content: typeof row.content === "string" ? row.content.slice(0, 800) : "",
        status: typeof row.status === "string" ? row.status.slice(0, 40) : "pending",
        priority: typeof row.priority === "string" ? row.priority.slice(0, 40) : "medium",
      }))
  }

  const normalizeTool = (tool: string): string => {
    let clean = tool.replace(/^mcp[.:_-]/i, "")
    clean = clean.replace(/^awoki[.:_-]/i, "")
    for (const candidate of sessionAwareTools) {
      if (clean === candidate || clean.endsWith(`_${candidate}`) || clean.endsWith(`.${candidate}`)) return candidate
    }
    return clean
  }

  const toolClass = (raw: string, normalized: string): string => {
    const lower = raw.toLowerCase()
    if (sessionAwareTools.has(normalized) || /^(?:mcp[.:_-])?awoki[.:_-]/i.test(raw)) return "awoki_mcp"
    if (lower.startsWith("mcp_") || lower.startsWith("mcp.") || lower.startsWith("mcp-") || lower.startsWith("mcp:")) return "other_mcp"
    if (nativeToolNames.has(normalized.toLowerCase())) return "native"
    return "native"
  }

  const clearTimer = (sid: string) => {
    const timer = timers.get(sid)
    if (timer) clearTimeout(timer)
    timers.delete(sid)
  }

  const timestampMs = (value: unknown): number => {
    if (typeof value !== "string" || !value.trim()) return Date.now()
    const parsed = Date.parse(value)
    return Number.isFinite(parsed) ? parsed : Date.now()
  }

  const continuationRecord = (result: Record<string, any>): Record<string, any> => {
    const value = result.continuation
    return value && typeof value === "object" ? value : {}
  }

  const explicitSessionIdle = async (sid: string): Promise<boolean> => {
    if (idleSessions.has(sid)) return true
    try {
      const sessionApi = client.session as any
      if (typeof sessionApi.status !== "function") return false
      const response = await sessionApi.status()
      const data = response?.data ?? response
      const state = data?.[sid]
      const label = String(state?.type ?? state?.status ?? state ?? "").toLowerCase()
      return label === "idle"
    } catch {
      return false
    }
  }

  const continuationPrompt = (record: Record<string, any>): string => {
    const id = String(record.continuation_id || "")
    const generation = Number(record.generation || 0)
    const workflow = String(record.workflow || "generic")
    return [
      "[Awoki best-effort continuation]",
      "A detached Awoki job reached a terminal state. This is a best-effort attempt to resume an already user-authorized workflow; readiness correctness does not depend on this conversation waking.",
      `Continuation: ${id} generation=${generation} workflow=${workflow}.`,
      "Use Awoki MCP only for workflow state and repository readiness.",
      "First call project_continuation_status and verify the same continuation/generation.",
      workflow === "repository-readiness" ? "Load the repository-readiness skill and follow its continuation contract." : "Continue only the recorded generic next action.",
      "Use the exact recorded project_id/repo/source_id explicitly. If no project is currently attached, do NOT create or attach one merely to resume; explicit name= is sufficient. If a different project is attached, stop and leave the continuation pending.",
      workflow === "repository-readiness" ? "Call repository_prepare_status exactly once for the recorded parent job; do not manually advance structural/vector child phases." : "Call the recorded detached-job status tool exactly once to verify the terminal result through MCP.",
      "Update the OpenCode todo list with todowrite so the waiting phase becomes completed/blocked/failed and any recorded follow-on goal remains visible.",
      workflow === "repository-readiness" ? "The parent job already owns all readiness transitions. Do not start another structural/vector refresh from this continuation." : "If the job completed and the recorded workflow truly requires another detached phase, advance only that recorded next action and checkpoint again.",
      "If FULL_READY is established, call project_continuation_finalize, mark the readiness todo complete, then continue the recorded resume_goal if it is non-empty.",
      "If the parent/job failed, was blocked/cancelled, configuration is blocked, or scope conflicts, do not retry automatically; cancel/finalize the continuation as appropriate and report the exact state.",
      "Do not use Bash/Grep/Read as a fallback, do not edit .env/config, do not clone/pull/checkout, and do not start duplicate refresh jobs.",
    ].join("\n")
  }

  const syncContinuation = async (sid: string): Promise<void> => {
    const result = await runBridge(["continuation-status", "--session-id", sid])
    if (result.status !== "ok") {
      clearTimer(sid)
      return
    }
    armContinuation(sid, continuationRecord(result))
  }

  const resumeIfIdle = async (sid: string): Promise<void> => {
    if (!(await explicitSessionIdle(sid))) {
      // Do not interrupt active/ad-hoc work. The next session.idle event will retry.
      clearTimer(sid)
      return
    }
    const claim = await runBridge(["continuation-claim", "--session-id", sid])
    if (claim.status !== "due") {
      if (claim.status === "scope_conflict") {
        clearTimer(sid)
        await log("info", "Continuation held because another project is attached", { sessionID: sid })
        return
      }
      const record = continuationRecord(claim)
      if (record && Object.keys(record).length) armContinuation(sid, record)
      return
    }
    const record = continuationRecord(claim)
    const generation = Number(record.generation || 0)
    idleSessions.delete(sid)
    clearTimer(sid)
    try {
      await client.session.prompt({
        path: { id: sid },
        body: { parts: [{ type: "text", text: continuationPrompt(record) }] },
      })
    } catch (error) {
      await log("warn", "Best-effort continuation prompt failed", { sessionID: sid, error: String(error) })
      await runBridge([
        "continuation-release", "--session-id", sid, "--generation", String(generation),
        "--retry-seconds", "60", "--reason", "opencode_prompt_failed",
      ])
      await syncContinuation(sid)
      return
    }
    const after = await runBridge(["continuation-status", "--session-id", sid])
    const afterRecord = continuationRecord(after)
    if (after.status === "ok" && Number(afterRecord.generation || 0) === generation && afterRecord.status === "claimed") {
      // Model returned without checkpointing/rescheduling/finalizing. Retry only a
      // bounded number of times; continuations.py enforces the attempt limit.
      await runBridge([
        "continuation-release", "--session-id", sid, "--generation", String(generation),
        "--retry-seconds", "60", "--reason", "resume_prompt_completed_without_checkpoint",
      ])
    }
    await syncContinuation(sid)
  }

  const pollContinuation = async (sid: string): Promise<void> => {
    const result = await runBridge(["continuation-poll", "--session-id", sid])
    const record = continuationRecord(result)
    if (result.status === "ready") {
      await resumeIfIdle(sid)
      return
    }
    if (result.status === "waiting" || result.status === "claimed" || result.status === "leased") {
      armContinuation(sid, record)
      return
    }
    clearTimer(sid)
  }

  function armContinuation(sid: string, record: Record<string, any>): void {
    clearTimer(sid)
    if (!sid || !record || record.auto_resume === false) return
    const status = String(record.status || "")
    if (["done", "cancelled", "blocked"].includes(status)) return
    if (status === "ready") {
      const due = timestampMs(record.not_before)
      const delay = Math.max(50, Math.min(60 * 60 * 1000, due - Date.now()))
      timers.set(sid, setTimeout(() => void resumeIfIdle(sid), delay))
      return
    }
    if (status === "waiting") {
      const due = timestampMs(record.not_before)
      const delay = Math.max(50, Math.min(60 * 60 * 1000, due - Date.now()))
      timers.set(sid, setTimeout(() => void pollContinuation(sid), delay))
      return
    }
    if (status === "claimed") {
      // Recover cleanly after an OpenCode/plugin restart or a prompt process dying
      // mid-claim. The Python state machine turns an expired lease back into ready.
      const due = timestampMs(record.lease_until)
      const delay = Math.max(50, Math.min(60 * 60 * 1000, due - Date.now()))
      timers.set(sid, setTimeout(() => void resumeIfIdle(sid), delay))
      return
    }
  }

  const restorePendingContinuations = async () => {
    const result = await runBridge(["continuation-pending"])
    const pending = Array.isArray(result.pending) ? result.pending : []
    for (const row of pending) {
      const sid = typeof row?.session_id === "string" ? row.session_id : ""
      if (!sid) continue
      armContinuation(sid, row.continuation || {})
    }
  }

  // Restore durable timers after OpenCode/plugin restart. A job may have finished
  // while OpenCode was closed; the first local poll observes that transition.
  setTimeout(() => void restorePendingContinuations(), 50)

  return {
    "tool.execute.before": async (input, output) => {
      const rawTool = String(input.tool || "")
      const tool = normalizeTool(rawTool)
      if (input.sessionID) idleSessions.delete(input.sessionID)
      if (input.sessionID && acceptanceSessions.has(input.sessionID) && !acceptanceControlTools.has(tool)) {
        await runBridge([
          "acceptance-tool", "--session-id", input.sessionID,
          "--tool", tool || rawTool || "unknown", "--tool-class", toolClass(rawTool, tool), "--phase", "started",
        ])
      }
      if (sessionAwareTools.has(tool) && input.sessionID && !output.args.session_id) {
        output.args.session_id = input.sessionID
      }
      if (projectOpenTools.has(tool) && input.sessionID) {
        const target = typeof output.args.name === "string" ? output.args.name.trim() : ""
        if (target) {
          await runBridge(["switch", "--session-id", input.sessionID, "--target-project", target])
        }
      }
    },

    "tool.execute.after": async (input) => {
      if (!input.sessionID) return
      const rawTool = String(input.tool || "")
      const tool = normalizeTool(rawTool)
      if (acceptanceObservableOrchestrationTools.has(tool)) {
        acceptanceSessions.add(input.sessionID)
      }
      if (acceptanceSessions.has(input.sessionID) && !acceptanceControlTools.has(tool)) {
        await runBridge([
          "acceptance-tool", "--session-id", input.sessionID,
          "--tool", tool || rawTool || "unknown", "--tool-class", toolClass(rawTool, tool), "--phase", "completed",
        ])
      }
      const mid = latestAssistantBySession.get(input.sessionID)
      if (mid) {
        const state = assistantTurns.get(mid)
        if (state) {
          state.toolExecutionsCompleted = Number(state.toolExecutionsCompleted || 0) + 1
          assistantTurns.set(mid, state)
        }
      }
      if (continuationTools.has(tool)) {
        await syncContinuation(input.sessionID)
        return
      }
      if (continuityMaintenanceTools.has(tool)) return
      await runBridge([
        "activity", "--session-id", input.sessionID, "--event", "tool.execute.after",
        "--tool", String(input.tool || "unknown"),
      ])
    },

    event: async ({ event }) => {
      const sid = sessionID(event)
      if (!sid) return
      if (event.type === "file.edited" || event.type === "file.watcher.updated") {
        idleSessions.delete(sid)
        await runBridge(["activity", "--session-id", sid, "--event", event.type, "--path", filePath(event)])
      } else if (event.type === "todo.updated") {
        await runBridge(["todo-sync", "--session-id", sid], { todos: eventTodos(event) })
      } else if (event.type === "message.updated") {
        idleSessions.delete(sid)
        const role = messageRole(event)
        const mid = messageID(event)
        if (role === "user") {
          const prior = latestAssistantBySession.get(sid)
          if (prior) assistantTurns.delete(prior)
          latestAssistantBySession.delete(sid)
          await runBridge(["user-turn", "--session-id", sid, "--message-id", mid])
        } else if (role === "assistant" && mid) {
          const prior = latestAssistantBySession.get(sid)
          if (prior && prior !== mid) assistantTurns.delete(prior)
          const current = assistantTurns.get(mid) ?? { messageID: mid, finish: "", hasReasoning: false, hasText: false, hasTool: false, providerID: "", modelID: "", agentMode: "", errorType: "", stepFinishSeen: false, inputTokens: 0, outputTokens: 0, reasoningTokens: 0, toolExecutionsCompleted: 0 }
          current.finish = messageFinish(event) || current.finish
          const info = messageInfo(event) as any
          current.providerID = String(info.providerID ?? info.providerId ?? info.provider_id ?? current.providerID ?? "")
          current.modelID = String(info.modelID ?? info.modelId ?? info.model_id ?? current.modelID ?? "")
          current.agentMode = String(info.mode ?? current.agentMode ?? "")
          const error = info.error && typeof info.error === "object" ? info.error : undefined
          if (error) current.errorType = String(error.name ?? error.type ?? error.code ?? error.data?.name ?? current.errorType ?? "")
          assistantTurns.set(mid, current)
          latestAssistantBySession.set(sid, mid)
        }
      } else if (event.type === "message.part.updated") {
        idleSessions.delete(sid)
        const part = eventPart(event) as any
        if (String(part.type ?? "").toLowerCase() === "compaction") {
          const trigger = part.auto === true
            ? "automatic_context_pressure"
            : part.auto === false
              ? "explicit_request"
              : "unknown"
          await runBridge([
            "compaction-trigger", "--session-id", sid,
            "--trigger", trigger, "--source", "message.part.updated:compaction.auto",
          ])
        }
        updatePartState(event)
      } else if (event.type === "session.updated" || event.type === "session.status") {
        idleSessions.delete(sid)
      } else if (event.type === "session.idle") {
        idleSessions.add(sid)
        await recordTerminalTurn(sid)
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.idle"])
        await syncContinuation(sid)
      } else if (event.type === "session.compacted") {
        await runBridge(["compacted", "--session-id", sid])
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.compacted", "--force"])
        await syncContinuation(sid)
      } else if (event.type === "session.deleted") {
        idleSessions.delete(sid)
        acceptanceSessions.delete(sid)
        const mid = latestAssistantBySession.get(sid)
        if (mid) assistantTurns.delete(mid)
        latestAssistantBySession.delete(sid)
        clearTimer(sid)
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.deleted", "--force", "--detach"])
        await runBridge(["continuation-cancel", "--session-id", sid, "--reason", "session_deleted"])
      }
    },

    "experimental.session.compacting": async (input, output) => {
      const sid = sessionID(input)
      if (!sid) return
      await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.compacting", "--force"])
      const result = await runBridge(["context", "--session-id", sid, "--max-chars", "24000"])
      const context = typeof result.context === "string" ? result.context : ""
      if (typeof result.acceptance_run_id === "string" && result.acceptance_run_id) acceptanceSessions.add(sid)
      if (context) output.context.push(context)
    },
  }
}
