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
    sessionID: string; messageID: string; finish: string; hasReasoning: boolean; hasText: boolean; hasTool: boolean;
    providerID: string; modelID: string; agentMode: string; errorType: string;
    stepFinishSeen: boolean; inputTokens: number; outputTokens: number; reasoningTokens: number;
    toolExecutionsCompleted: number;
    parentMessageID?: string; isSummary?: boolean;
    infoSeen?: boolean; partsSeen?: boolean; lookupAttempted?: boolean; terminalRecorded?: boolean;
    summaryParentVerified?: boolean; textCompleteSeen?: boolean;
    compactionContinuation?: CompactionWitness;
    activityVersion: number;
  }
  type CompactionWitness = {
    userMessageID: string; summaryMessageID?: string; markerMessageID?: string;
    compacted: boolean; parentMessageID?: string; summaryCompletedAt?: number;
  }
  const compactions = new Map<string, CompactionWitness>()
  const pendingUserChecks = new Map<string, Set<object>>()
  const eventUserQueues = new Map<string, Promise<void>>()
  const eventUserEpochs = new Map<string, object>()
  const assistantTurns = new Map<string, AssistantTurnState>()
  const latestAssistantBySession = new Map<string, string>()
  const latestUserBySession = new Map<string, string>()
  const seenUsersBySession = new Map<string, Set<string>>()
  const idleLookups = new Map<string, Promise<void>>()
  const userRegistrations = new Map<string, Promise<Record<string, any>>>()
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

  const newAssistantState = (sid: string, mid: string): AssistantTurnState => ({
    sessionID: sid, messageID: mid, finish: "", hasReasoning: false, hasText: false, hasTool: false,
    providerID: "", modelID: "", agentMode: "", errorType: "", stepFinishSeen: false,
    inputTokens: 0, outputTokens: 0, reasoningTokens: 0, toolExecutionsCompleted: 0,
    activityVersion: 0,
  })

  const observeAssistant = (sid: string, mid: unknown, activity = true): AssistantTurnState | undefined => {
    if (!sid || typeof mid !== "string" || !mid || mid === latestUserBySession.get(sid)) return
    const existing = assistantTurns.get(mid)
    if (existing && existing.sessionID !== sid) return
    const prior = latestAssistantBySession.get(sid)
    if (prior && prior !== mid) assistantTurns.delete(prior)
    const current = existing ?? newAssistantState(sid, mid)
    if (activity) {
      idleSessions.delete(sid)
      current.activityVersion++
      current.lookupAttempted = false
    }
    assistantTurns.set(mid, current)
    latestAssistantBySession.set(sid, mid)
    return current
  }

  const rememberNativeUser = (sid: string, mid: string) => {
    const seen = seenUsersBySession.get(sid) ?? new Set<string>()
    seen.add(mid)
    seenUsersBySession.set(sid, seen)
  }

  const registerUser = async (sid: string, info: any, fromEvent = false) => {
    const mid = info?.id
    if (!sid || info?.sessionID !== sid || info?.role !== "user" || typeof mid !== "string" || !mid) return
    const seen = seenUsersBySession.get(sid) ?? new Set<string>()
    if (seen.has(mid)) return
    compactions.delete(sid)
    if (!fromEvent) {
      eventUserEpochs.set(sid, {})
      pendingUserChecks.delete(sid)
    }
    seen.add(mid)
    seenUsersBySession.set(sid, seen)
    idleSessions.delete(sid)
    const prior = latestAssistantBySession.get(sid)
    if (prior) assistantTurns.delete(prior)
    latestAssistantBySession.delete(sid)
    latestUserBySession.set(sid, mid)
    const priorRegistration = userRegistrations.get(sid)
    const registration = (async () => {
      // Hook/event handlers can overlap. Preserve observed user order in the
      // durable bridge even when an earlier process completes slowly.
      await priorRegistration
      return runBridge(["user-turn", "--session-id", sid, "--message-id", mid])
    })()
    userRegistrations.set(sid, registration)
    await registration
  }

  const syntheticContinuation = (data: any, sid: string, mid: string): boolean => Boolean(
    data?.info?.id === mid && data.info.sessionID === sid && data.info.role === "user"
    && Array.isArray(data.parts) && data.parts.length > 0 && data.parts.length <= 16
    && data.parts.every((part: any) => part && part.type === "text" && typeof part.text === "string"
      && part.sessionID === sid && part.messageID === mid && part.synthetic === true
      && part.metadata?.compaction_continue === true),
  )

  const registerEventUser = async (sid: string, info: any) => {
    if (seenUsersBySession.get(sid)?.has(info?.id)) return
    // Native marker users can arrive before the compacting hook, even in an
    // established session. Classify each unseen event-only user by exact parts;
    // real chat hooks and already-classified duplicate IDs remain fetch-free.
    if (info?.sessionID !== sid || info.role !== "user" || typeof info.id !== "string" || !info.id) {
      compactions.delete(sid)
      return
    }
    const epoch = eventUserEpochs.get(sid) ?? {}
    eventUserEpochs.set(sid, epoch)
    const check = {}
    const pending = pendingUserChecks.get(sid) ?? new Set<object>()
    pending.add(check)
    pendingUserChecks.set(sid, pending)
    const prior = eventUserQueues.get(sid)
    const queued = (async () => {
      await prior
      if (eventUserEpochs.get(sid) !== epoch || seenUsersBySession.get(sid)?.has(info.id)) return
      let classified = false
      try {
        const response = await client.session.message({path: {id: sid, messageID: info.id}, query: {directory}})
        if (eventUserEpochs.get(sid) !== epoch) return
        const data = response?.data
        if (data?.info?.id !== info.id || data.info.sessionID !== sid || data.info.role !== "user"
            || !Array.isArray(data.parts) || !data.parts.length || data.parts.length > 256
            || data.parts.some((part: any) => !part || typeof part.type !== "string"
              || part.sessionID !== sid || part.messageID !== info.id)) return
        const marker = data.parts.every((part: any) => part.type === "compaction")
        if (syntheticContinuation(data, sid, info.id) || marker) {
          rememberNativeUser(sid, info.id)
          const witness = compactions.get(sid)
          if (marker && witness?.markerMessageID && witness.markerMessageID !== info.id) compactions.delete(sid)
          classified = true
          return
        }
        if (data.parts.some((part: any) => part.type === "compaction" || part.synthetic === true
            || (part.type === "text" && typeof part.text !== "string"))) return
        await registerUser(sid, data.info, true)
        classified = true
      } catch {
        // Unknown user provenance cannot authorize completion of the older human.
      } finally {
        if (!classified && eventUserEpochs.get(sid) === epoch) compactions.delete(sid)
      }
    })()
    eventUserQueues.set(sid, queued)
    try { await queued } finally {
      pending.delete(check)
      if (!pending.size && pendingUserChecks.get(sid) === pending) pendingUserChecks.delete(sid)
      if (eventUserQueues.get(sid) === queued) eventUserQueues.delete(sid)
    }
  }

  const updateMessageState = (sid: string, info: any, activity = true) => {
    if (info?.sessionID !== sid || info?.role !== "assistant") return
    const current = observeAssistant(sid, info.id, activity)
    if (!current) return
    current.infoSeen = true
    current.finish = messageFinish({ info }) || current.finish
    current.parentMessageID = typeof info.parentID === "string" ? info.parentID : current.parentMessageID
    current.isSummary = info.summary === true || current.isSummary === true
    current.providerID = String(info.providerID ?? info.providerId ?? info.provider_id ?? current.providerID ?? "")
    current.modelID = String(info.modelID ?? info.modelId ?? info.model_id ?? current.modelID ?? "")
    current.agentMode = String(info.mode ?? current.agentMode ?? "")
    const error = info.error && typeof info.error === "object" ? info.error : undefined
    if (error) current.errorType = String(error.name ?? error.type ?? error.code ?? error.data?.name ?? current.errorType ?? "")
  }

  const updatePartState = (value: unknown, activity = true) => {
    const mid = partMessageID(value)
    const sid = sessionID(value)
    if (!mid || !sid) return
    const current = observeAssistant(sid, mid, activity)
    if (!current) return
    current.partsSeen = true
    const part = eventPart(value) as any
    const type = String(part.type ?? "").toLowerCase()
    if (type === "reasoning" || type.includes("reasoning")) current.hasReasoning = true
    else if (type === "text" && typeof part.text === "string" && part.text.trim()) current.hasText = true
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

  const terminalMetadataReady = (state: AssistantTurnState | undefined): boolean => Boolean(
    state?.infoSeen && state.parentMessageID && (state.finish || state.errorType || state.isSummary)
    && (state.partsSeen || state.isSummary || state.errorType),
  )

  const recoverTerminalMetadata = async (sid: string) => {
    // Older hosts deliver message/part updates; newer hosts may only deliver
    // content deltas. Fetch one exact observed ID, never the session transcript.
    while (idleLookups.has(sid)) await idleLookups.get(sid)
    if (!idleSessions.has(sid)) return
    const mid = latestAssistantBySession.get(sid) || ""
    const user = latestUserBySession.get(sid) || ""
    const state = assistantTurns.get(mid)
    const needsParentVerification = state && state.parentMessageID !== user
      && !(state.isSummary && state.summaryParentVerified) && !state.compactionContinuation
    if (!state || (terminalMetadataReady(state) && !needsParentVerification) || state.lookupAttempted) return
    const version = state.activityVersion
    state.lookupAttempted = true
    const stillCurrent = () => !pendingUserChecks.has(sid) && idleSessions.has(sid) && state.activityVersion === version && latestAssistantBySession.get(sid) === mid
      && latestUserBySession.get(sid) === (user || undefined) && assistantTurns.get(mid) === state
    const lookup = (async () => {
      try {
        const response = await client.session.message({ path: { id: sid, messageID: mid }, query: { directory } })
        const data = response?.data
        const info = data?.info
        if (!stillCurrent() || !info || info.id !== mid || info.sessionID !== sid || info.role !== "assistant"
            || typeof info.parentID !== "string" || !info.parentID || !Array.isArray(data.parts)
            || data.parts.some(part => !part || typeof part !== "object" || typeof part.type !== "string"
              || part.sessionID !== sid || part.messageID !== mid
              || ((part.type === "text" || part.type === "reasoning") && typeof part.text !== "string")
              || (part.type === "tool" && (!part.state || typeof part.state !== "object"
                || !["pending", "running", "completed", "error"].includes(part.state.status))))) return
        let summaryParentVerified = false
        let continuation: CompactionWitness | undefined
        if (info.summary === true && info.parentID !== user) {
          // Native compaction can have a synthetic user parent. Validate only
          // that exact identity; do not count it as a user turn or infer trigger.
          const parentResponse = await client.session.message({
            path: { id: sid, messageID: info.parentID }, query: { directory },
          })
          const parent = parentResponse?.data?.info
          if (!stillCurrent() || parent?.id !== info.parentID || parent.sessionID !== sid || parent.role !== "user") return
          summaryParentVerified = true
        } else if (!user) return
        else if (info.parentID !== user) {
          const witness = compactions.get(sid)
          if (!witness?.compacted || witness.userMessageID !== user || !witness.summaryMessageID
              || !witness.markerMessageID || !Number.isFinite(witness.summaryCompletedAt) || (witness.parentMessageID && witness.parentMessageID !== info.parentID)
              || info.parentID === witness.markerMessageID || mid === witness.summaryMessageID) return
          const parentResponse = await client.session.message({
            path: {id: sid, messageID: info.parentID}, query: {directory},
          })
          const parentCreated = parentResponse?.data?.info?.time?.created
          const answerCreated = info.time?.created
          if (!stillCurrent() || compactions.get(sid) !== witness
              || !syntheticContinuation(parentResponse?.data, sid, info.parentID)
              || typeof parentCreated !== "number" || !Number.isFinite(parentCreated)
              || parentCreated < witness.summaryCompletedAt!
              || typeof answerCreated !== "number" || !Number.isFinite(answerCreated) || answerCreated < parentCreated) return
          witness.parentMessageID = info.parentID
          rememberNativeUser(sid, info.parentID)
          rememberNativeUser(sid, witness.markerMessageID)
          continuation = witness
        }
        if (!stillCurrent()) return
        const fresh = newAssistantState(sid, mid)
        fresh.lookupAttempted = true
        fresh.partsSeen = true  // The exact API returned the complete parts array, including an empty array.
        fresh.summaryParentVerified = summaryParentVerified
        fresh.compactionContinuation = continuation
        assistantTurns.set(mid, fresh)
        updateMessageState(sid, info, false)
        for (const part of data.parts) updatePartState({ part }, false)
        fresh.toolExecutionsCompleted = data.parts.filter(part => part.type === "tool" && part.state.status === "completed").length
      } catch {
        // SDK/API errors do not justify a guessed terminal receipt or transcript scan.
      }
    })()
    idleLookups.set(sid, lookup)
    try { await lookup } finally { if (idleLookups.get(sid) === lookup) idleLookups.delete(sid) }
  }

  const recordTerminalTurn = async (sid: string) => {
    if (!idleSessions.has(sid)) return
    let registration: Promise<Record<string, any>> | undefined
    do {
      registration = userRegistrations.get(sid)
      await registration
      if (!idleSessions.has(sid)) return
    } while (registration !== userRegistrations.get(sid))
    const user = latestUserBySession.get(sid)
    await recoverTerminalMetadata(sid)
    if (!idleSessions.has(sid) || pendingUserChecks.has(sid) || latestUserBySession.get(sid) !== user) return
    const mid = latestAssistantBySession.get(sid) || ""
    const state = mid ? assistantTurns.get(mid) : undefined
    if (!state || state.terminalRecorded || !terminalMetadataReady(state) || state.sessionID !== sid) return
    if (state.parentMessageID !== latestUserBySession.get(sid)
        && !(state.isSummary && state.summaryParentVerified)
        && !(state.compactionContinuation && compactions.get(sid) === state.compactionContinuation
          && state.compactionContinuation.userMessageID === user)) return
    state.terminalRecorded = true
    const args = [
      "agent-turn-terminal", "--session-id", sid, "--message-id", state.messageID,
      "--parent-message-id", state.parentMessageID || "",
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
    if (state.isSummary) args.push("--is-summary")
    if (state.compactionContinuation && !state.isSummary) {
      args.push("--compaction-continuation-of", state.compactionContinuation.userMessageID,
        "--compaction-summary-message-id", state.compactionContinuation.summaryMessageID || "",
        "--compaction-marker-message-id", state.compactionContinuation.markerMessageID || "")
      // One terminal receipt consumes this observed continuation, not a reusable
      // license to attribute other synthetic users to the original human.
      compactions.delete(sid)
    }
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
    "chat.message": async (input, output) => {
      const parts = output.parts
      if (parts?.length && (parts.every((part: any) => part.type === "compaction")
          || parts.every((part: any) => part.type === "text" && part.synthetic === true
            && part.metadata?.compaction_continue === true))) return
      await registerUser(input.sessionID, output.message)
    },

    "experimental.text.complete": async (input) => {
      const state = observeAssistant(input.sessionID, input.messageID)
      if (state) state.textCompleteSeen = true
    },

    "tool.execute.before": async (input, output) => {
      const rawTool = String(input.tool || "")
      const tool = normalizeTool(rawTool)
      // Some host versions supply a top-level messageID on tool hooks. Never
      // derive identity from tool arguments, output text or arbitrary metadata.
      observeAssistant(input.sessionID, (input as any).messageID)
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
      observeAssistant(input.sessionID, (input as any).messageID)
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
          await registerEventUser(sid, messageInfo(event))
        } else if (role === "assistant" && mid) {
          updateMessageState(sid, messageInfo(event))
        }
      } else if (String(event.type) === "message.part.delta") {
        idleSessions.delete(sid)
        observeAssistant(sid, (event as any)?.properties?.messageID)
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
      } else if (event.type === "session.status") {
        // Session metadata updates can follow idle while exact reads are in
        // flight. Only execution status and actual activity invalidate idle.
        idleSessions.delete(sid)
      } else if (event.type === "session.idle") {
        idleSessions.add(sid)
        await recordTerminalTurn(sid)
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.idle"])
        await syncContinuation(sid)
      } else if (event.type === "session.compacted") {
        const witness = compactions.get(sid)
        if (witness?.summaryMessageID && witness.markerMessageID
            && witness.userMessageID === latestUserBySession.get(sid)) witness.compacted = true
        await runBridge(["compacted", "--session-id", sid])
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.compacted", "--force"])
        await syncContinuation(sid)
      } else if (event.type === "session.deleted") {
        idleSessions.delete(sid)
        acceptanceSessions.delete(sid)
        const mid = latestAssistantBySession.get(sid)
        if (mid) assistantTurns.delete(mid)
        latestAssistantBySession.delete(sid)
        latestUserBySession.delete(sid)
        seenUsersBySession.delete(sid)
        userRegistrations.delete(sid)
        compactions.delete(sid)
        pendingUserChecks.delete(sid)
        eventUserEpochs.delete(sid)
        eventUserQueues.delete(sid)
        clearTimer(sid)
        await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.deleted", "--force", "--detach"])
        await runBridge(["continuation-cancel", "--session-id", sid, "--reason", "session_deleted"])
      }
    },

    "experimental.compaction.autocontinue": async (input, output) => {
      const sid = input.sessionID
      const witness = compactions.get(sid)
      const summaryID = latestAssistantBySession.get(sid)
      const marker = input.message
      if (!witness || output.enabled !== true || !summaryID || !assistantTurns.get(summaryID)?.textCompleteSeen || witness.summaryMessageID
          || marker?.role !== "user" || marker.sessionID !== sid || !marker.id
          || latestUserBySession.get(sid) !== witness.userMessageID) return
      try {
        // This native hook runs after the summary and before the synthetic user.
        // Bind its exact marker to the observed summary; no text is persisted.
        const response = await client.session.message({path: {id: sid, messageID: summaryID}, query: {directory}})
        const info = response?.data?.info
        if (compactions.get(sid) !== witness || latestUserBySession.get(sid) !== witness.userMessageID
            || latestAssistantBySession.get(sid) !== summaryID || pendingUserChecks.has(sid)
            || info?.id !== summaryID || info.sessionID !== sid || info.role !== "assistant"
            || info.summary !== true || info.parentID !== marker.id || info.error
            || !["stop", "end_turn"].includes(messageFinish({info}))
            || typeof info.time?.completed !== "number" || !Number.isFinite(info.time.completed)) return
        witness.summaryCompletedAt = info.time.completed
        witness.summaryMessageID = summaryID
        witness.markerMessageID = marker.id
        rememberNativeUser(sid, marker.id)
      } catch {
        // Missing hook/API evidence leaves synthetic-parent attribution unknown.
      }
    },

    "experimental.session.compacting": async (input, output) => {
      const sid = sessionID(input)
      if (!sid) return
      const user = latestUserBySession.get(sid)
      if (user) compactions.set(sid, {userMessageID: user, compacted: false})
      else compactions.delete(sid)
      await runBridge(["checkpoint", "--session-id", sid, "--reason", "session.compacting", "--force"])
      const result = await runBridge(["context", "--session-id", sid, "--max-chars", "24000"])
      const context = typeof result.context === "string" ? result.context : ""
      if (typeof result.acceptance_run_id === "string" && result.acceptance_run_id) acceptanceSessions.add(sid)
      if (context) output.context.push(context)
    },
  }
}
