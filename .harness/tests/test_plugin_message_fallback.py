"""Execute the actual continuity plugin hooks with deterministic exact-message APIs."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = r'''
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const [pluginPath, scenario] = process.argv.slice(2);
const bridge = [], calls = [], logs = [], payloads = [];
const failures = new Map();
const responseOverrides = new Map();
let invalidResponse = "", hangingCommand = "", killed = 0;
let delayRecovery = false, releaseRecovery;
const safeRecovery = "Awoki recovery snapshot: verified scope; no saved goal is valid exploration. Follow the newest user request. Read exact note details only if needed.";
if (scenario === "bridge_timeout" || scenario === "logging_timeout" || scenario === "recovery_timeout") {
  const schedule = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms, ...args) => schedule(fn, [15000, 2000].includes(ms) ? 5 : ms, ...args);
}
let delayFirstUser = false, releaseFirstUser, durableUser = "";
globalThis.Bun = { spawn: (args, options) => {
  bridge.push(args.slice(2));
  const command = args[2];
  if (command === "todo-sync") payloads.push(options.stdin.text().then(JSON.parse));
  else assert.equal(options.stdin, "ignore", "These hooks must send no private payload");
  const failed = (failures.get(command) || 0) > 0;
  if (failed) failures.set(command, failures.get(command) - 1);
  let exited = Promise.resolve(failed ? 1 : 0);
  if (command === hangingCommand) exited = new Promise(() => {});
  if (command === "recovery-context" && delayRecovery) exited = new Promise(resolve => {releaseRecovery = () => resolve(0);});
  if (command === "user-turn" && !failed && command !== hangingCommand && command !== invalidResponse) {
    const id = args[args.indexOf("--message-id") + 1];
    if (delayFirstUser && id === "u1") exited = new Promise(resolve => { releaseFirstUser = () => {durableUser = id; resolve(0);}; });
    else durableUser = id;
  }
  if (args[2] === "agent-turn-terminal" && !args.includes("--is-summary"))
    assert.equal(args[args.indexOf(args.includes("--compaction-continuation-of") ? "--compaction-continuation-of" : "--parent-message-id") + 1], durableUser, "Terminal must follow the latest durable user registration");
  const mid = args[args.indexOf("--message-id") + 1];
  const isSummary = args.includes("--is-summary");
  const response = command === "user-turn" ? {status: "marked", agent_runtime: {
      status: "user_turn_recorded", current_turn: {user_message_id: mid}}}
    : command === "agent-turn-terminal" ? {status: isSummary ? "summary_recorded" : "recorded",
      [isSummary ? "last_compaction_turn" : "last_terminal_turn"]: {
        message_id: mid, parent_message_id: args[args.indexOf("--parent-message-id") + 1], is_summary: isSummary}}
    : command === "recovery-context" ? {status: "ok", session_id: args[args.indexOf("--session-id") + 1], context: safeRecovery}
    : {};
  return {stdout: new Response(command === invalidResponse ? "not-json"
      : responseOverrides.has(command) ? responseOverrides.get(command) : JSON.stringify(response)).body,
    stderr: new Response("").body, exited, kill: () => {killed++;}};
}};
let lookup = async () => { throw new Error("Unexpected exact lookup"); };
let active = 0, maxActive = 0;
const { AwokiContinuity } = await import(pathToFileURL(pluginPath).href);
const client = {
  app: {log: async value => {
    logs.push(value);
    if (scenario === "logging_timeout") return new Promise(() => {});
  }},
  session: {message: async request => {
    calls.push(request); active++; maxActive = Math.max(maxActive, active);
    try { return await lookup(request); } finally { active--; }
  }},
};
const hooks = await AwokiContinuity({directory: "/owned/project", client});
const sid = "session-current";
const emit = (type, properties) => hooks.event({event: {type, properties}});
const user = async (id = "u1", target = sid) => hooks["chat.message"](
  {sessionID: target, messageID: "input-is-not-authoritative"},
  {message: {id, sessionID: target, role: "user"}, parts: [{type: "text", text: "PRIVATE_USER_CANARY"}]},
);
const info = (id = "a1", parentID = "u1", extra = {}) => ({
  id, sessionID: sid, role: "assistant", parentID, finish: "stop", providerID: "fixture",
  modelID: "local-fixture", mode: "build", time: {created: 1, completed: 2}, ...extra,
});
const part = (type, extra = {}, mid = "a1") => ({id: `part-${type}`, type, sessionID: sid, messageID: mid, ...extra});
const exact = (id = "a1", parentID = "u1", extra = {}, parts = [part("text", {text: "PRIVATE_ANSWER_CANARY"}, id)]) =>
  ({data: {info: info(id, parentID, extra), parts}});
const delta = (mid = "a1") => emit("message.part.delta", {
  sessionID: sid, messageID: mid, partID: "part-private", field: "text", delta: "PRIVATE_DELTA_CANARY",
});
const idle = () => emit("session.idle", {sessionID: sid});
const terminals = () => bridge.filter(args => args[0] === "agent-turn-terminal");
const generations = () => bridge.filter(args => args[0] === "user-turn");
const arg = (args, name) => args[args.indexOf(name) + 1];
const until = async predicate => { for (let i=0; i<50 && !predicate(); i++) await new Promise(resolve => setImmediate(resolve)); assert.ok(predicate()); };

const nativeUser = (id, parts, extra = {}) => ({data: {
  info: {id, sessionID: sid, role: "user", time: {created: 2}, ...extra}, parts,
}});
const syntheticUser = (extra = {}) => nativeUser("synthetic", [part("text", {
  text: "PRIVATE_SYNTHETIC_CANARY", synthetic: true, metadata: {compaction_continue: true}, ...extra,
}, "synthetic")]);
const summaryData = () => exact("summary", "marker", {summary: true});
const nativeLookup = async request => request.path.messageID === "summary" ? summaryData()
  : request.path.messageID === "synthetic" ? syntheticUser()
  : request.path.messageID === "marker" ? nativeUser("marker", [part("compaction", {auto: true}, "marker")])
  : exact("answer", "synthetic", {time: {created: 3, completed: 4}});
const compacting = () => hooks["experimental.session.compacting"]({sessionID: sid}, {context: []});
const summaryText = () => hooks["experimental.text.complete"]({sessionID: sid, messageID: "summary", partID: "summary-text"}, {text: "PRIVATE_SUMMARY_CANARY"});
const autocontinue = (message = {id: "marker", sessionID: sid, role: "user"}, enabled = true) =>
  hooks["experimental.compaction.autocontinue"]({sessionID: sid, message, overflow: false}, {enabled});
const compacted = () => emit("session.compacted", {sessionID: sid});
const witness = async (uid = "u1") => {
  await user(uid); await compacting(); await summaryText(); lookup = nativeLookup;
  await autocontinue(); await compacted();
};
const recoveryMessages = (summaryID = "saved-summary", userID = "resumed-user") => [
  {info: info(summaryID, "marker", {summary: true}), parts: [part("text", {text: "PRIVATE_SUMMARY_CANARY"}, summaryID)]},
  nativeUser(userID, [part("text", {text: "PRIVATE_NEWEST_REQUEST_CANARY"}, userID)]).data,
];
const transform = async messages => {
  await hooks["experimental.chat.messages.transform"]({}, {messages});
  return messages;
};

if (scenario === "recovery_retained_tail") {
  for (const retainedTail of [false, true]) {
    const summaryID = "summary-tail-" + retainedTail;
    const summary = recoveryMessages(summaryID)[0];
    const original = structuredClone(summary), originalParts = summary.parts;
    const marker = nativeUser("marker", [part("compaction", {auto: false}, "marker")]).data;
    const rows = [marker, summary, ...(retainedTail ? [exact("retained-answer", "old-human").data] : [])];
    const before = bridge.length;
    await transform(rows);
    assert.equal(rows.length, retainedTail ? 3 : 2, "Fallback cannot invent a user turn");
    assert.equal(rows[1].parts.length, 2);
    assert.notEqual(rows[1], summary, "Only the outgoing row is replaced");
    assert.equal(summary.parts, originalParts);
    assert.deepEqual(summary, original, "Original summary body and canonical part objects remain untouched");
    assert.equal(rows[1].parts.at(-1).messageID, summaryID);
    assert.equal(rows[1].parts.at(-1).text, safeRecovery);
    assert.deepEqual(bridge.slice(before), [["recovery-context", "--session-id", sid]], "Fallback performs one local state read only");
    await transform(rows); assert.equal(rows[1].parts.length, 2, "Fallback remains once per summary");
  }
} else if (scenario === "recovery_retained_tail_invalid") {
  for (const defect of ["no-marker", "wrong-parent", "real-human", "new-marker", "summarizer-head"]) {
    const summary = recoveryMessages("invalid-" + defect)[0];
    const marker = nativeUser("marker", [part("compaction", {auto: false}, "marker")]).data;
    let rows = [marker, summary, exact("retained-answer", "old-human").data];
    if (defect === "no-marker") rows.shift();
    if (defect === "wrong-parent") summary.info.parentID = "different-marker";
    if (defect === "real-human") marker.parts.push(part("text", {text: "PRIVATE_USER_CANARY"}, "marker"));
    if (defect === "new-marker") rows.push(nativeUser("next-marker", [part("compaction", {auto: true}, "next-marker")]).data);
    if (defect === "summarizer-head") rows = [exact("old-answer", "old-human").data];
    const original = structuredClone(rows); await transform(rows);
    assert.deepEqual(rows, original, "Only an exact completed native summary pair permits fallback");
  }
} else if (scenario === "compaction_reference_framing") {
  responseOverrides.set("context", JSON.stringify({context: "Saved direction and exact record IDs."}));
  const output = {context: []}; await hooks["experimental.session.compacting"]({sessionID: sid}, output);
  assert.equal(output.context.length, 1);
  assert.match(output.context[0], /^Awoki reference state for summarization only\./);
  assert.match(output.context[0], /Tools are unavailable here/);
  assert.match(output.context[0], /do not execute tools or emit tool-call markup/);
  assert.ok(output.context[0].includes("Saved direction and exact record IDs."));
  assert.match(output.context[0], /perform recovery calls on the next investigation turn\.$/);
  const request = bridge.filter(row => row[0] === "context").at(-1);
  const budget = Number(arg(request, "--max-chars"));
  assert.ok(budget < 24000 && budget > 23000);
  responseOverrides.set("context", JSON.stringify({context: "x".repeat(budget)}));
  const full = {context: []}; await hooks["experimental.session.compacting"]({sessionID: sid}, full);
  assert.equal(full.context[0].length, 24000, "Framing fits inside the existing total context limit");
} else if (scenario === "recovery_reminder") {
  for (const mode of ["synthetic", "overflow-replay", "new-human"]) {
    const messages = recoveryMessages("summary-" + mode, mode);
    if (mode === "synthetic") {
      messages[1] = syntheticUser().data;
    } else if (mode === "new-human") {
      messages.splice(1, 0, syntheticUser().data);
    }
    const before = bridge.length;
    await transform(messages);
    const reminder = messages.at(-1).parts.at(-1);
    assert.equal(reminder.type, "text");
    assert.equal(reminder.synthetic, true);
    assert.equal(reminder.messageID, messages.at(-1).info.id);
    assert.equal(reminder.text, safeRecovery);
    assert.ok(!reminder.text.includes("session_work_status"), "Verified no-goal snapshot requires no extra model call");
    assert.deepEqual(bridge.slice(before), [["recovery-context", "--session-id", sid]], "Snapshot must use only one local state read");
    const fresh = recoveryMessages("summary-" + mode, "next-tool-loop-user");
    await transform(fresh);
    assert.equal(fresh.at(-1).parts.length, 1, "One reminder per summary, not every tool iteration");
  }
} else if (scenario === "recovery_concurrent") {
  delayRecovery = true;
  const first = recoveryMessages(), duplicate = recoveryMessages();
  const pending = transform(first); await until(() => Boolean(releaseRecovery));
  await transform(duplicate);
  assert.equal(duplicate.at(-1).parts.length, 1, "Concurrent duplicate must not deliver unverified context");
  assert.equal(bridge.filter(row => row[0] === "recovery-context").length, 1);
  releaseRecovery(); await pending;
  assert.equal(first.at(-1).parts.at(-1).text, safeRecovery);
  await transform(duplicate);
  assert.equal(duplicate.at(-1).parts.length, 1, "Only one delivery per summary");
} else if (scenario === "recovery_races") {
  for (const race of ["human", "deleted", "scope", "todo", "acceptance", "pending-user", "resolved-unknown", "maintenance", "replacement", "new-marker", "mutated-identity"]) {
    delayRecovery = true; releaseRecovery = undefined;
    const summaryID = "race-" + race;
    const rows = recoveryMessages(summaryID);
    const output = {messages: rows};
    const pending = hooks["experimental.chat.messages.transform"]({}, output);
    await until(() => Boolean(releaseRecovery));
    let finishLookup, userCheck;
    if (race === "human") await user("new-human");
    if (race === "resolved-unknown") {
      lookup = async () => ({});
      await emit("message.updated", {info: {id: "resolved-unknown", sessionID: sid, role: "user"}});
    }
    if (race === "maintenance") await hooks["tool.execute.after"]({sessionID: sid, tool: "awoki_project_capture"});
    if (race === "deleted") await emit("session.deleted", {info: {id: sid}});
    if (race === "scope") await hooks["tool.execute.before"]({sessionID: sid, tool: "awoki_project_open"}, {args: {name: "new-project"}});
    if (race === "todo") await emit("todo.updated", {sessionID: sid, todos: []});
    if (race === "acceptance") await hooks["tool.execute.before"]({sessionID: sid, tool: "awoki_acceptance_run_start"}, {args: {}});
    if (race === "pending-user") {
      lookup = async () => new Promise(resolve => {finishLookup = () => resolve({});});
      userCheck = emit("message.updated", {info: {id: "unknown-user", sessionID: sid, role: "user"}});
      await until(() => Boolean(finishLookup));
    }
    if (race === "replacement") output.messages = recoveryMessages(summaryID);
    if (race === "new-marker") rows.push(nativeUser("next-marker", [part("compaction", {auto: true}, "next-marker")]).data);
    if (race === "mutated-identity") rows.at(-1).info.id = "changed-target";
    releaseRecovery(); await pending;
    assert.equal(rows[1].parts.length, 1, race + ": stale snapshot must not be delivered");
    if (userCheck) {finishLookup(); await userCheck;}
    delayRecovery = false;
    if (race !== "deleted") {
      const retry = recoveryMessages(summaryID);
      const reads = bridge.filter(row => row[0] === "recovery-context").length;
      await transform(retry);
      assert.match(retry.at(-1).parts.at(-1).text, /recovery is unknown/);
      assert.equal(bridge.filter(row => row[0] === "recovery-context").length, reads, "Raced state must not trigger repeated reads");
    }
  }
} else if (scenario === "recovery_unknown") {
  const responses = ["{}", "not-json", "[]", JSON.stringify({status: "ok", context: safeRecovery}),
    JSON.stringify({status: "ok", session_id: "wrong-session", context: safeRecovery}),
    JSON.stringify({status: "complete", session_id: sid, context: safeRecovery}),
    JSON.stringify({status: "ok", session_id: sid, context: " "}),
    JSON.stringify({status: "ok", session_id: sid, context: "x".repeat(2501)}),
  ];
  for (const [index, response] of responses.entries()) {
    responseOverrides.set("recovery-context", response);
    const rows = recoveryMessages("unknown-" + index); await transform(rows);
    const text = rows.at(-1).parts.at(-1).text;
    assert.match(text, /scope, work and acceptance state could not be verified/);
    assert.match(text, /Do not infer no goal, completion or unrestricted actions/);
    assert.ok(!text.includes("session_work_status"), "Unknown acceptance cannot silently authorize generic recovery");
    await transform(recoveryMessages("unknown-" + index));
  }
  assert.equal(bridge.filter(row => row[0] === "recovery-context").length, responses.length);
  responseOverrides.delete("recovery-context"); failures.set("recovery-context", 1);
  const failed = recoveryMessages("failed-exit"); await transform(failed);
  assert.match(failed.at(-1).parts.at(-1).text, /recovery is unknown/, "A failed process cannot deliver its apparently valid stdout");
  const partial = "Verified scope; work:unknown; goal:saved; read exact cont_fixture by project_search.";
  responseOverrides.set("recovery-context", JSON.stringify({status: "ok", session_id: sid, context: partial, untrusted_extra: "PRIVATE_PAYLOAD_CANARY"}));
  const rows = recoveryMessages("partial"); await transform(rows);
  assert.equal(rows.at(-1).parts.at(-1).text, partial, "Explicit partial-state warnings remain intact");
  responseOverrides.set("recovery-context", JSON.stringify({status: "unknown", session_id: sid, context: "Unknown acceptance; recover authoritative state first."}));
  const unknown = recoveryMessages("backend-unknown"); await transform(unknown);
  assert.equal(unknown.at(-1).parts.at(-1).text, "Unknown acceptance; recover authoritative state first.");
} else if (scenario === "recovery_timeout") {
  hangingCommand = "recovery-context";
  const rows = recoveryMessages(); await transform(rows);
  assert.match(rows.at(-1).parts.at(-1).text, /recovery is unknown/);
  assert.equal(killed, 1);
  for (let index = 0; index < 4; index++) await transform(recoveryMessages());
  assert.equal(bridge.filter(row => row[0] === "recovery-context").length, 1, "Timeout delivers unknown once without retry loop");
} else if (scenario === "recovery_acceptance") {
  const context = "Verified active acceptance run fixture-run. Use acceptance_run_next before other actions.";
  responseOverrides.set("recovery-context", JSON.stringify({status: "ok", session_id: sid, context, acceptance_run_id: "fixture-run"}));
  const rows = recoveryMessages(); await transform(rows);
  assert.equal(rows.at(-1).parts.at(-1).text, context);
  assert.ok(!rows.at(-1).parts.at(-1).text.includes("session_work_status"));
  await hooks["tool.execute.before"]({sessionID: sid, tool: "read"}, {args: {path: "synthetic-fixture"}});
  assert.equal(bridge.filter(row => row[0] === "acceptance-tool").length, 1, "Snapshot restores native acceptance bookkeeping after restart");
} else if (scenario === "recovery_invalid") {
  const cases = [
    [], [nativeUser("human", []).data], recoveryMessages().slice(0, 1),
    (() => { const rows = recoveryMessages(); rows[0].info.error = {name: "aborted"}; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[0].info.time.completed = undefined; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[0].info.finish = "length"; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[1].info.sessionID = "other"; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[1].parts[0].sessionID = "other"; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[1].parts[0].messageID = "other"; return rows; })(),
    (() => { const rows = recoveryMessages(); rows[1].parts.push(part("compaction", {auto: true}, "resumed-user")); return rows; })(),
    (() => { const rows = recoveryMessages(); rows.push({info: info("incomplete-summary", "resumed-user", {summary: true, finish: ""}), parts: []}); return rows; })(),
  ];
  for (const rows of cases) {
    const original = structuredClone(rows);
    await transform(rows);
    assert.deepEqual(rows, original, "Unknown/failed/mixed-session/summarizer input must remain unchanged");
  }
  const valid = recoveryMessages(); await transform(valid);
  assert.equal(valid.at(-1).parts.length, 2, "Rejected input must not consume recovery eligibility");
} else if (scenario === "recovery_restart") {
  const first = recoveryMessages(); await transform(first);
  const second = await AwokiContinuity({directory: "/owned/project", client});
  await second["experimental.chat.messages.transform"]({}, {messages: first});
  assert.equal(first.at(-1).parts.length, 2, "Duplicate plugin registration cannot add another reminder");
  const third = await AwokiContinuity({directory: "/owned/project", client});
  const resumed = recoveryMessages();
  await third["experimental.chat.messages.transform"]({}, {messages: resumed});
  assert.equal(resumed.at(-1).parts.length, 2, "Restart re-derives recovery from actual summary identity");
  assert.equal(calls.length, 0, "Recovery detection must not scan or fetch transcript text");
} else if (scenario === "failed_user_retry" || scenario === "logging_timeout") {
  failures.set("user-turn", 1);
  await user();
  assert.equal(durableUser, "");
  await emit("message.updated", {info: {id: "u1", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 2);
  await delta(); lookup = async () => exact();
  await idle(); await user();
  assert.equal(terminals().length, 1);
  assert.equal(generations().length, 2, "Acknowledged users remain idempotent");
} else if (scenario === "failed_user_bounded") {
  failures.set("user-turn", 20);
  await user(); await delta(); lookup = async () => exact();
  for (let i=0; i<5; i++) { await idle(); await user(); }
  assert.equal(generations().length, 2, "Broken storage permits only one event-driven retry");
  assert.equal(terminals().length, 0, "Unacknowledged human cannot receive a terminal receipt");
  assert.equal(calls.length, 0, "Do not fetch terminal metadata until persistence succeeds");
} else if (scenario === "failed_old_user") {
  failures.set("user-turn", 1);
  await user(); await user("u2"); await user("u1");
  await delta("a2"); lookup = async () => exact("a2", "u2"); await idle();
  assert.deepEqual(generations().map(row => arg(row, "--message-id")), ["u1", "u2"]);
  assert.equal(arg(terminals()[0], "--parent-message-id"), "u2");
} else if (scenario === "failed_terminal_retry" || scenario === "failed_compaction_terminal_retry") {
  if (scenario === "failed_compaction_terminal_retry") { await witness(); await delta("answer"); }
  else { await user(); await delta(); lookup = async () => exact(); }
  failures.set("agent-turn-terminal", 1);
  await idle(); await Promise.all([idle(), idle()]); await idle();
  assert.equal(terminals().length, 2);
  assert.deepEqual(terminals()[0], terminals()[1], "Retry preserves the exact identity and native compaction witness");
} else if (scenario === "failed_terminal_bounded") {
  await user(); await delta(); lookup = async () => exact();
  failures.set("agent-turn-terminal", 20);
  for (let i=0; i<5; i++) await idle();
  assert.equal(terminals().length, 2);
} else if (scenario === "bridge_timeout" || scenario === "invalid_bridge_response") {
  if (scenario === "bridge_timeout") hangingCommand = "user-turn";
  else invalidResponse = "user-turn";
  await user();
  assert.equal(durableUser, "");
  hangingCommand = ""; invalidResponse = "";
  await user(); await delta(); lookup = async () => exact(); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(generations().length, 2);
  if (scenario === "bridge_timeout") assert.equal(killed, 1);
} else if (scenario === "malformed_todos") {
  const valid = {id: "t1", content: "Inspect exact saved reference", status: "pending", priority: "medium"};
  await emit("todo.updated", {sessionID: sid, todos: [valid]});
  for (const todos of [null, undefined, {}, [null], ["bad"], [{...valid, content: 7}],
      [{...valid, status: "unknown"}], [{...valid, priority: "unknown"}], [{...valid, content: " "}]])
    await emit("todo.updated", {sessionID: sid, todos});
  await emit("todo.updated", {sessionID: sid, todos: []});
  assert.deepEqual(await Promise.all(payloads), [{todos: [{...valid,
    content_truncated: false}], todos_omitted: 0}, {todos: [], todos_omitted: 0}]);
} else if (scenario === "todo_truncation_metadata") {
  const refs = Array.from({length: 10}, (_, n) => "cont_saved_" + n);
  const valid = {id: "t1", content: "x".repeat(810) + " " + refs.join(" "), status: "pending", priority: "medium"};
  await emit("todo.updated", {sessionID: sid, todos: Array.from({length: 70}, (_, n) => ({...valid, id: "t" + n}))});
  const payload = (await Promise.all(payloads))[0];
  assert.equal(payload.todos.length, 64);
  assert.equal(payload.todos_omitted, 6);
  assert.equal(payload.todos[0].content, valid.content, "Bridge receives references after800 within original context for canonical redaction");
  assert.equal(payload.todos[0].content_truncated, false);
  assert.ok(!("record_refs" in payload.todos[0]), "Plugin must not extract potential credential-shaped reference strings");
  assert.ok(!("record_refs_omitted" in payload.todos[0]));
  await emit("todo.updated", {sessionID: sid, todos: [{...valid, content: "x".repeat(8300)}]});
  const oversized = (await Promise.all(payloads))[1];
  assert.equal(oversized.todos[0].content.length, 8192);
  assert.equal(oversized.todos[0].content_truncated, true);
} else if (scenario === "unacknowledged_bridge_status") {
  const invalid = ["", "{}", '{"status":"ignored"}', '{"status":"marked"}',
    JSON.stringify({status: "marked", agent_runtime: {status: "user_turn_recorded", current_turn: {user_message_id: "other"}}})];
  for (let n=0; n<invalid.length; n++) {
    responseOverrides.set("user-turn", invalid[n]);
    await user("invalid-user-" + n);
    responseOverrides.delete("user-turn");
    const before = generations().length;
    await user("invalid-user-" + n);
    assert.equal(generations().length, before + 1, "Exit zero alone must not consume the durable-user guard");
  }
  await user("terminal-user"); await delta("terminal-answer");
  lookup = async () => exact("terminal-answer", "terminal-user");
  responseOverrides.set("agent-turn-terminal", "{}"); await idle();
  responseOverrides.delete("agent-turn-terminal"); await idle(); await idle();
  assert.equal(terminals().length, 2, "Empty successful terminal output is unavailable, allowing one retry");
} else if (scenario === "duplicate_bridge_status") {
  responseOverrides.set("user-turn", JSON.stringify({status: "unchanged", agent_runtime: {
    status: "duplicate", current_turn: {user_message_id: "u1"}}}));
  await user(); await user();
  assert.equal(generations().length, 1);
  await delta(); lookup = async () => exact();
  responseOverrides.set("agent-turn-terminal", JSON.stringify({status: "duplicate",
    last_terminal_turn: {message_id: "a1", parent_message_id: "u1", is_summary: false}}));
  await idle(); await idle();
  assert.equal(terminals().length, 1, "Exact duplicate receipt is a durable acknowledgment");
} else if (scenario === "sdk_session_deleted") {
  await user(); await emit("session.deleted", {info: {id: sid}});
  const deletion = bridge.find(row => row[0] === "continuation-cancel");
  assert.equal(arg(deletion, "--session-id"), sid);
  const before = bridge.length;
  await emit("message.updated", {info: {id: "not-a-session", role: "assistant"}});
  assert.equal(bridge.length, before, "Generic IDs must never be interpreted as session identity");
  await delta(); lookup = async () => exact(); await idle();
  assert.equal(terminals().length, 0, "Deletion clears human attribution");
} else if (scenario === "session_metadata_after_idle") {
  for (const phase of ["answer", "synthetic"]) {
    await witness("human-" + phase); await delta("answer");
    let release;
    lookup = request => request.path.messageID === phase
      ? new Promise(resolve => {release = resolve;}) : nativeLookup(request);
    const before = terminals().length;
    const pending = idle(); await until(() => release);
    // CLI 1.18 emits session metadata after idle while exact API reads remain
    // in flight. It changes no message or execution status.
    await emit("session.updated", {sessionID: sid, info: {id: sid, time: {updated: 5}}});
    release(await nativeLookup({path: {messageID: phase}})); await pending;
    assert.equal(terminals().length, before + 1, "Session metadata must not erase idle completion");
    assert.equal(arg(terminals().at(-1), "--compaction-continuation-of"), "human-" + phase);
    assert.equal(arg(terminals().at(-1), "--parent-message-id"), "synthetic");
  }
} else if (scenario === "real_activity_after_idle") {
  for (const activity of ["busy", "retry", "new-human", "new-part"]) {
    await witness("human-" + activity); await delta("answer");
    let release;
    lookup = request => request.path.messageID === "answer"
      ? new Promise(resolve => {release = resolve;}) : nativeLookup(request);
    const pending = idle(); await until(() => release);
    if (activity === "new-human") await user("replacement-human");
    else if (activity === "new-part") await delta("new-answer");
    else await emit("session.status", {sessionID: sid, status: {type: activity}});
    release(await nativeLookup({path: {messageID: "answer"}})); await pending;
    assert.equal(terminals().length, 0, "Actual activity must invalidate in-flight idle attribution");
  }
} else if (scenario === "normal") {
  await user();
  await emit("message.updated", {info: {id: "u1", sessionID: sid, role: "user"}});
  await emit("message.updated", {info: info()});
  await emit("message.part.updated", {part: part("reasoning", {text: "PRIVATE_REASONING_CANARY"})});
  await emit("message.part.updated", {part: part("text", {text: "PRIVATE_ANSWER_CANARY"})});
  await emit("message.part.updated", {part: part("step-finish", {reason: "stop", tokens: {input: 10, output: 8, reasoning: 4}})});
  await Promise.all([idle(), idle()]);
  assert.equal(calls.length, 0);
  assert.equal(generations().length, 1);
  assert.equal(terminals().length, 1);
  assert.ok(terminals()[0].includes("--has-text"));
  assert.ok(terminals()[0].includes("--has-reasoning"));
  assert.equal(arg(terminals()[0], "--parent-message-id"), "u1");
  // The traditional summary event path remains fetch-free and distinct.
  await emit("message.updated", {info: info("summary", "u1", {summary: true})});
  await idle();
  assert.equal(calls.length, 0);
  assert.ok(terminals().at(-1).includes("--is-summary"));
} else if (scenario === "fallback") {
  await user();
  await delta();
  await hooks["experimental.text.complete"]({sessionID: sid, messageID: "a1", partID: "text"}, {text: "PRIVATE_ANSWER_CANARY"});
  lookup = async request => {
    assert.deepEqual(request, {path: {id: sid, messageID: "a1"}, query: {directory: "/owned/project"}});
    return exact("a1", "u1", {}, [
      part("reasoning", {text: "PRIVATE_REASONING_CANARY"}),
      part("text", {text: "PRIVATE_ANSWER_CANARY"}),
      part("tool", {callID: "call1", state: {status: "completed", input: {value: "PRIVATE_ARGUMENT_CANARY"}, output: "PRIVATE_TOOL_CANARY"}}),
      part("step-finish", {reason: "stop", tokens: {input: 20, output: 12, reasoning: 3}}),
    ]);
  };
  await Promise.all([idle(), idle()]);
  await idle();
  assert.equal(calls.length, 1);
  assert.equal(terminals().length, 1);
  assert.ok(terminals()[0].includes("--has-text"));
  assert.ok(terminals()[0].includes("--has-reasoning"));
  assert.ok(terminals()[0].includes("--has-tool"));
  assert.equal(arg(terminals()[0], "--tool-executions-completed"), "1");
  assert.equal(arg(terminals()[0], "--input-tokens"), "20");
} else if (scenario === "tool_and_empty") {
  await user();
  await hooks["tool.execute.after"]({sessionID: sid, messageID: "a1", tool: "project_capture", callID: "call1", args: {value: "PRIVATE_ARGUMENT_CANARY"}});
  lookup = async () => exact("a1", "u1", {finish: "tool-calls"}, [
    part("tool", {state: {status: "completed", input: {value: "PRIVATE_ARGUMENT_CANARY"}, output: "PRIVATE_TOOL_CANARY"}}),
  ]);
  await idle();
  assert.equal(terminals().length, 1);
  assert.ok(terminals()[0].includes("--has-tool"));
  assert.ok(!terminals()[0].includes("--has-text"));
  await user("u2");
  await hooks["experimental.text.complete"]({sessionID: sid, messageID: "a2", partID: "empty"}, {text: "  "});
  lookup = async () => exact("a2", "u2", {}, [part("text", {text: "  "}, "a2")]);
  await idle();
  assert.equal(terminals().length, 2);
  assert.ok(!terminals().at(-1).includes("--has-text"));
} else if (scenario === "invalid") {
  const malformed = [
    {data: {...exact().data, info: info("wrong-id")}},
    {data: {...exact().data, info: info("a1", "u1", {sessionID: "foreign-session"})}},
    {data: {...exact().data, info: {id: "a1", sessionID: sid, role: "user"}}},
    exact("a1", "older-user"),
    exact("a1", ""),
    exact("a1", "u1", {}, [part("text", {text: "PRIVATE_FOREIGN_CANARY", sessionID: "foreign-session"})]),
    exact("a1", "u1", {}, [part("text", {text: "PRIVATE_FOREIGN_CANARY"}, "wrong-message")]),
    exact("a1", "u1", {}, [part("text", {text: "PRIVATE_ANSWER_CANARY"}), part("tool", {state: undefined})]),
    {error: {message: "PRIVATE_API_ERROR_CANARY"}}, {},
    exact("a1", "u1", {finish: ""}),
    "throw",
  ];
  for (let index = 0; index < malformed.length; index++) {
    const uid = "u" + (index + 1), aid = "invalid-" + index;
    await user(uid); await delta(aid);
    lookup = async () => {
      const value = malformed[index];
      if (value === "throw") throw new Error("PRIVATE_API_ERROR_CANARY");
      // Correct the fixture IDs unless that field is the deliberately invalid one.
      if (value.data?.info?.id === "a1") value.data.info.id = aid;
      if (value.data?.info?.parentID === "u1") value.data.info.parentID = uid;
      for (const p of value.data?.parts || []) if (p.messageID === "a1") p.messageID = aid;
      return value;
    };
    const count = calls.length;
    await idle(); await idle();
    assert.equal(calls.length, count + 1, "Failed lookup must not loop on duplicate idle");
    assert.equal(terminals().length, 0, "Invalid identity or incomplete API state cannot produce a receipt");
  }
} else if (scenario === "summary") {
  await user(); await delta("summary");
  lookup = async request => request.path.messageID === "summary"
    ? exact("summary", "synthetic-parent", {summary: true})
    : {data: {info: {id: "synthetic-parent", sessionID: sid, role: "user"}, parts: [
        {type: "compaction", sessionID: sid, messageID: "synthetic-parent", auto: false},
      ]}};
  await idle();
  assert.equal(calls.length, 2);
  assert.equal(generations().length, 1, "Compaction parent is not a new actual user turn");
  assert.ok(terminals()[0].includes("--is-summary"));
  assert.equal(bridge.filter(args => args[0] === "compaction-trigger").length, 0);
  await user("u2"); await delta("bad-summary");
  lookup = async request => request.path.messageID === "bad-summary"
    ? exact("bad-summary", "other-parent", {summary: true})
    : {data: {info: {id: "other-parent", sessionID: "foreign-session", role: "user"}, parts: []}};
  await idle();
  assert.equal(terminals().length, 1, "Foreign compaction parent must not be accepted");
} else if (scenario === "concurrent_new_user") {
  await user(); await delta();
  let release;
  lookup = request => request.path.messageID === "a1"
    ? new Promise(resolve => {release = resolve;}) : Promise.resolve(exact("a2", "u2"));
  const firstIdle = idle();
  await until(() => calls.length === 1);
  const duplicateIdle = idle();
  await user("u2"); await delta("a2");
  // Delayed duplicate delivery of the old user must not roll back the new turn.
  await emit("message.updated", {info: {id: "u1", sessionID: sid, role: "user"}});
  release(exact());
  await Promise.all([firstIdle, duplicateIdle]);
  assert.equal(terminals().length, 0, "Old idle handlers cannot finish the active new turn");
  assert.equal(calls.length, 1, "The active new message must wait for its own idle");
  await idle();
  assert.equal(maxActive, 1, "Concurrent idle hooks must serialize exact API reads");
  assert.equal(calls.length, 2);
  assert.equal(generations().length, 2);
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--message-id"), "a2");
  assert.equal(arg(terminals()[0], "--parent-message-id"), "u2");
} else if (scenario === "no_candidate") {
  await hooks["chat.message"]({sessionID: sid}, {message: {id: "u1", sessionID: "foreign", role: "user"}, parts: []});
  await idle();
  assert.equal(generations().length, 0);
  assert.equal(calls.length, 0);
  assert.equal(terminals().length, 0);
} else if (scenario === "user_registration_order") {
  delayFirstUser = true;
  const first = user();
  await until(() => generations().length === 1);
  const second = user("u2");
  await delta("a2");
  lookup = async () => exact("a2", "u2");
  const pendingIdle = idle();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(generations().length, 1, "Second registration must wait for first process completion");
  assert.equal(calls.length, 0, "Idle must wait for the newest durable user registration");
  releaseFirstUser();
  await Promise.all([first, second, pendingIdle]);
  assert.deepEqual(generations().map(args => arg(args, "--message-id")), ["u1", "u2"]);
  assert.equal(durableUser, "u2");
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--parent-message-id"), "u2");
} else if (scenario === "compaction_continuation") {
  await witness(); await delta("answer");
  await idle(); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(calls.length, 3, "Exactly summary, observed answer, and its native parent are read");
  const receipt = terminals()[0];
  assert.equal(arg(receipt, "--parent-message-id"), "synthetic");
  assert.equal(arg(receipt, "--compaction-continuation-of"), "u1");
  assert.equal(arg(receipt, "--compaction-summary-message-id"), "summary");
  assert.equal(arg(receipt, "--compaction-marker-message-id"), "marker");
  assert.equal(generations().length, 1);
  assert.equal(bridge.filter(args => args[0] === "compaction-trigger").length, 0);
  // Consumed witness cannot authorize another synthetic user's response.
  await delta("other-answer");
  lookup = async () => exact("other-answer", "other-synthetic");
  await idle(); assert.equal(terminals().length, 1);
  await emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  await emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 1, "Delayed native user events do not become human generations");
} else if (scenario === "compaction_invalid_parent") {
  const values = [
    nativeUser("synthetic", []),
    nativeUser("synthetic", syntheticUser().data.parts, {time: {created: 1}}),
    nativeUser("synthetic", syntheticUser().data.parts, {time: {}}),
    nativeUser("synthetic", syntheticUser().data.parts, {time: {created: 4}}),
    syntheticUser({synthetic: false}),
    syntheticUser({metadata: {}}), syntheticUser({sessionID: "foreign"}),
    syntheticUser({messageID: "other"}), syntheticUser({text: null}),
    nativeUser("synthetic", syntheticUser().data.parts, {sessionID: "foreign"}),
    nativeUser("synthetic", [...syntheticUser().data.parts, part("text", {text: "PRIVATE_HUMAN_CANARY"}, "synthetic")]),
    nativeUser("synthetic", [...syntheticUser().data.parts, part("tool", {state: {status: "completed"}}, "synthetic")]),
  ];
  for (let i=0; i<values.length; i++) {
    await witness("human-" + i); await delta("answer");
    lookup = async request => request.path.messageID === "synthetic" ? values[i] : nativeLookup(request);
    await idle(); await idle();
    assert.equal(terminals().length, 0, "Synthetic flag alone cannot attribute completion");
  }
} else if (scenario === "compaction_missing_witness") {
  for (const missing of ["compacting", "text-complete", "autocontinue", "compacted", "enabled", "foreign-marker", "bad-summary", "delta-only"]) {
    await user("human-" + missing); lookup = nativeLookup;
    if (missing !== "compacting") await compacting();
    if (missing !== "text-complete" && missing !== "delta-only") await summaryText();
    if (missing === "delta-only") await delta("summary");
    if (missing === "bad-summary") lookup = async request => request.path.messageID === "summary"
      ? exact("summary", "other-marker", {summary: true}) : nativeLookup(request);
    if (missing !== "autocontinue") await autocontinue(
      {id: "marker", role: "user", sessionID: missing === "foreign-marker" ? "foreign" : sid}, missing !== "enabled");
    if (missing !== "compacted") await compacted();
    lookup = nativeLookup;
    // Full old-host event metadata must not bypass continuation verification.
    await emit("message.updated", {info: info("answer", "synthetic", {time: {created: 3, completed: 4}})});
    await emit("message.part.updated", {part: part("text", {text: "PRIVATE_ANSWER_CANARY"}, "answer")});
    await idle();
    assert.equal(terminals().length, 0, missing);
  }
} else if (scenario === "compaction_native_user_events") {
  await witness();
  await emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  await emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  await hooks["chat.message"]({sessionID: sid}, {message: syntheticUser().data.info, parts: syntheticUser().data.parts});
  assert.equal(generations().length, 1);
  await emit("message.updated", {info: info("answer", "synthetic", {time: {created: 3, completed: 4}})});
  await emit("message.part.updated", {part: part("text", {text: "PRIVATE_ANSWER_CANARY"}, "answer")});
  await idle();
  assert.equal(terminals().length, 1, "Legacy complete metadata still validates exact native parent");
  assert.equal(arg(terminals()[0], "--compaction-continuation-of"), "u1");
} else if (scenario === "compaction_new_user_races") {
  for (const phase of ["summary", "parent", "delete", "event-user"]) {
    await user("original-" + phase); await compacting(); await summaryText();
    let release;
    if (phase === "summary") {
      lookup = () => new Promise(resolve => { release = resolve; });
      const pending = autocontinue(); await until(() => release);
      await user("new-summary"); release(summaryData()); await pending; await compacted();
      lookup = nativeLookup; await delta("answer"); await idle();
    } else {
      lookup = nativeLookup; await autocontinue(); await compacted(); await delta("answer");
      lookup = request => request.path.messageID === "synthetic"
        ? new Promise(resolve => {release = resolve;}) : nativeLookup(request);
      const pending = idle(); await until(() => release);
      if (phase === "delete") await emit("session.deleted", {sessionID: sid});
      else if (phase === "event-user") {
        lookup = async () => nativeUser("new-event", [part("text", {text: "PRIVATE_NEW_USER_CANARY"}, "new-event")]);
        await emit("message.updated", {info: {id: "new-event", sessionID: sid, role: "user"}});
      } else await user("new-parent");
      release(syntheticUser()); await pending;
    }
    assert.equal(terminals().length, 0, "A stale witness cannot complete a newer user or deleted session");
  }
} else if (scenario === "fresh_instance_initial_user") {
  lookup = async () => { throw new Error("PRIVATE_API_ERROR_CANARY"); };
  await emit("message.updated", {info: {id: "unreadable", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 0, "A fresh instance cannot infer a human from an unreadable role-only event");
  lookup = async () => syntheticUser();
  await emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 0);
  lookup = async () => nativeUser("initial-human", [part("text", {text: "PRIVATE_HUMAN_CANARY"}, "initial-human")]);
  await emit("message.updated", {info: {id: "initial-human", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 1);
  const count = calls.length;
  lookup = async () => nativeUser("next-human", [part("text", {text: "PRIVATE_NEXT_HUMAN_CANARY"}, "next-human")]);
  await emit("message.updated", {info: {id: "next-human", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 2);
  assert.equal(calls.length, count + 1, "An unseen event-only user is classified even in an established session");
  await emit("message.updated", {info: {id: "next-human", sessionID: sid, role: "user"}});
  assert.equal(calls.length, count + 1, "Known duplicate user IDs remain fetch-free");
} else if (scenario === "early_compaction_marker") {
  await user("human");
  let release;
  lookup = () => new Promise(resolve => {release = resolve;});
  const first = emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  await until(() => release || generations().length > 1);
  assert.equal(generations().length, 1, "An early marker event must not register a second human");
  const duplicate = emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  // OpenCode 1.18 can publish marker user.info before its parts and before
  // the compacting hook. The delayed exact read must not create a human turn.
  await emit("message.part.updated", {part: part("compaction", {auto: true}, "marker")});
  await compacting();
  release(await nativeLookup({path: {messageID: "marker"}}));
  await Promise.all([first, duplicate]);
  assert.equal(calls.length, 1, "Duplicate initial classification is serialized and cached");
  assert.equal(generations().length, 1, "The compaction marker cannot replace the registered human");
  lookup = nativeLookup; await summaryText(); await autocontinue(); await compacted();
  await delta("answer"); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--compaction-continuation-of"), "human");
  assert.equal(arg(terminals()[0], "--parent-message-id"), "synthetic");
} else if (scenario === "early_unknown_and_new_chat") {
  await user("human-1");
  lookup = async () => nativeUser("marker", []);
  await emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  assert.equal(generations().length, 1, "Role-only event before parts must remain unclassified");
  let release;
  lookup = () => new Promise(resolve => {release = resolve;});
  const pending = emit("message.updated", {info: {id: "late-event-human", sessionID: sid, role: "user"}});
  await until(() => release);
  await user("human-2");
  release(nativeUser("late-event-human", [part("text", {text: "PRIVATE_OLD_HUMAN_CANARY"}, "late-event-human")]));
  await pending;
  assert.deepEqual(generations().map(row => arg(row, "--message-id")), ["human-1", "human-2"]);
  lookup = async () => exact("answer-2", "human-2");
  await delta("answer-2"); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--parent-message-id"), "human-2");
} else if (scenario === "two_plugin_instances") {
  await witness("human");
  const second = await AwokiContinuity({directory: "/owned/project", client});
  await second.event({event: {type: "message.updated", properties: {info: syntheticUser().data.info}}});
  assert.equal(generations().length, 1, "Fresh duplicate instance cannot register synthetic U as human");
  await delta("answer"); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--compaction-continuation-of"), "human");
  assert.equal(arg(terminals()[0], "--parent-message-id"), "synthetic");
  await second.event({event: {type: "message.updated", properties: {info: syntheticUser().data.info}}});
  assert.equal(generations().length, 1);
} else if (scenario === "compaction_queued_user_checks") {
  await witness("human-1");
  let release;
  lookup = request => request.path.messageID === "human-2"
    ? new Promise(resolve => {release = resolve;}) : nativeLookup(request);
  const humanCheck = emit("message.updated", {info: {id: "human-2", sessionID: sid, role: "user"}});
  await until(() => release);
  const nativeCheck = emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  await delta("answer"); await idle();
  assert.equal(terminals().length, 0, "All queued user checks block old continuation completion");
  assert.equal(calls.filter(row => row.path.messageID === "synthetic").length, 0, "Classification is serialized in event order");
  release(nativeUser("human-2", [part("text", {text: "PRIVATE_NEW_HUMAN_CANARY"}, "human-2")]));
  await Promise.all([humanCheck, nativeCheck]);
  assert.deepEqual(generations().map(row => arg(row, "--message-id")), ["human-1", "human-2"]);
  await delta("answer"); await idle();
  assert.equal(terminals().length, 0);
  await delta("new-answer"); lookup = async () => exact("new-answer", "human-2"); await idle();
  assert.equal(terminals().length, 1);
  assert.equal(arg(terminals()[0], "--parent-message-id"), "human-2");
} else if (scenario === "compaction_user_replay") {
  await witness("human-1");
  await emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  await user("human-2");
  const reads = calls.length;
  await emit("message.updated", {info: {id: "synthetic", sessionID: sid, role: "user"}});
  await emit("message.updated", {info: {id: "marker", sessionID: sid, role: "user"}});
  assert.deepEqual(generations().map(row => arg(row, "--message-id")), ["human-1", "human-2"]);
  assert.equal(calls.length, reads, "Verified native IDs remain ignored after witness invalidation");
} else if (scenario === "compaction_unknown_user") {
  for (const value of [{}, "throw", nativeUser("unknown", []), nativeUser("unknown", [
    part("text", {text: "PRIVATE_HUMAN_CANARY"}, "unknown"),
    part("text", {text: "PRIVATE_SYNTHETIC_CANARY", synthetic: true}, "unknown"),
  ])]) {
    await witness("human-" + calls.length);
    lookup = async () => { if (value === "throw") throw new Error("PRIVATE_ERROR_CANARY"); return value; };
    await emit("message.updated", {info: {id: "unknown", sessionID: sid, role: "user"}});
    lookup = nativeLookup; await delta("answer"); await idle();
    assert.equal(terminals().length, 0, "An unknown new user invalidates the continuation witness");
  }
  await witness("old-human");
  await hooks["chat.message"]({sessionID: sid}, {message: {id: "augmented-human", sessionID: sid, role: "user"}, parts: [
    {type: "text", text: "PRIVATE_HUMAN_CANARY"},
    {type: "text", text: "PRIVATE_SYNTHETIC_CANARY", synthetic: true},
  ]});
  assert.equal(arg(generations().at(-1), "--message-id"), "augmented-human");
  lookup = nativeLookup; await delta("answer"); await idle();
  assert.equal(terminals().length, 0);
} else if (scenario === "compaction_summary_separate") {
  await witness();
  await idle();
  assert.equal(terminals().length, 1);
  assert.ok(terminals()[0].includes("--is-summary"));
  assert.ok(!terminals()[0].includes("--compaction-continuation-of"));
  await delta("answer"); await idle();
  assert.equal(terminals().length, 2);
  assert.equal(arg(terminals()[1], "--compaction-continuation-of"), "u1");
} else throw new Error("Unknown scenario");
assert.ok(!JSON.stringify({bridge, logs}).includes("PRIVATE_"), "Private message/tool content reached persisted metadata or logs");
console.log(JSON.stringify({scenario, exact_reads: calls.length, terminal_receipts: terminals().length}));
'''


@unittest.skipUnless(shutil.which("node"), "Node required for actual TypeScript hook execution")
class PluginMessageFallbackTests(unittest.TestCase):
    def run_scenario(self, scenario):
        # tests/ lives inside .harness; project root is its third parent.
        plugin = Path(__file__).resolve().parents[2] / ".opencode/plugins/awoki-continuity.ts"
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "exercise-message-hooks.mjs"
            script.write_text(SCRIPT)
            result = subprocess.run([shutil.which("node"), "--experimental-strip-types", str(script), str(plugin), scenario],
                env={"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}, cwd=directory,
                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovery_concurrent_transforms_read_and_deliver_once(self):
        self.run_scenario("recovery_concurrent")

    def test_recovery_discards_changed_user_scope_transcript_and_deleted_session(self):
        self.run_scenario("recovery_races")

    def test_recovery_invalid_receipts_are_unknown_and_partial_warnings_survive(self):
        self.run_scenario("recovery_unknown")

    def test_recovery_timeout_is_unknown_without_retry_loop(self):
        self.run_scenario("recovery_timeout")

    def test_recovery_preserves_restricted_acceptance_and_restores_bookkeeping(self):
        self.run_scenario("recovery_acceptance")

    def test_transient_recovery_covers_native_replay_and_new_user_without_looping(self):
        self.run_scenario("recovery_reminder")

    def test_retained_assistant_tail_recovers_without_creating_or_mutating_canonical_message(self):
        self.run_scenario("recovery_retained_tail")

    def test_retained_tail_fallback_requires_exact_summary_marker_pair(self):
        self.run_scenario("recovery_retained_tail_invalid")

    def test_compaction_frames_reference_state_and_keeps_existing_context_budget(self):
        self.run_scenario("compaction_reference_framing")

    def test_recovery_rejects_failed_compaction_mixed_scope_and_summarizer_input(self):
        self.run_scenario("recovery_invalid")

    def test_plugin_restart_recovers_from_summary_identity_without_transcript_reads(self):
        self.run_scenario("recovery_restart")

    def test_failed_user_write_can_retry_without_reclassifying_the_human(self):
        self.run_scenario("failed_user_retry")

    def test_unavailable_logging_cannot_hang_recovery_after_bridge_failure(self):
        self.run_scenario("logging_timeout")

    def test_failed_user_write_is_bounded_and_blocks_terminal_attribution(self):
        self.run_scenario("failed_user_bounded")

    def test_old_failed_user_cannot_replay_over_newer_direction(self):
        self.run_scenario("failed_old_user")

    def test_terminal_write_retries_once_and_then_is_idempotent(self):
        self.run_scenario("failed_terminal_retry")

    def test_failed_terminal_write_preserves_exact_compaction_witness(self):
        self.run_scenario("failed_compaction_terminal_retry")

    def test_terminal_write_failures_cannot_loop(self):
        self.run_scenario("failed_terminal_bounded")

    def test_hung_bridge_is_killed_and_does_not_consume_acknowledgment(self):
        self.run_scenario("bridge_timeout")

    def test_invalid_bridge_json_does_not_consume_acknowledgment(self):
        self.run_scenario("invalid_bridge_response")

    def test_malformed_todo_payload_does_not_clear_valid_work(self):
        self.run_scenario("malformed_todos")

    def test_todo_bridge_transports_bounded_original_for_canonical_redaction(self):
        self.run_scenario("todo_truncation_metadata")

    def test_bridge_exit_zero_without_persisted_status_does_not_acknowledge(self):
        self.run_scenario("unacknowledged_bridge_status")

    def test_exact_duplicate_bridge_receipts_acknowledge_idempotently(self):
        self.run_scenario("duplicate_bridge_status")

    def test_sdk_session_deletion_shape_is_scoped_to_lifecycle_events(self):
        self.run_scenario("sdk_session_deleted")

    def test_post_idle_session_metadata_preserves_exact_continuation_lookups(self):
        self.run_scenario("session_metadata_after_idle")

    def test_actual_activity_still_invalidates_exact_idle_lookups(self):
        self.run_scenario("real_activity_after_idle")

    def test_regular_events_need_no_exact_lookup_and_register_user_once(self):
        self.run_scenario("normal")

    def test_delta_and_text_hook_fallback_records_only_structural_metadata(self):
        self.run_scenario("fallback")

    def test_tool_identity_and_empty_text_do_not_claim_an_answer(self):
        self.run_scenario("tool_and_empty")

    def test_identity_mismatches_missing_data_and_api_errors_fail_closed(self):
        self.run_scenario("invalid")

    def test_synthetic_compaction_parent_is_scoped_without_user_or_trigger_inference(self):
        self.run_scenario("summary")

    def test_concurrent_idle_and_new_user_discard_delayed_old_message(self):
        self.run_scenario("concurrent_new_user")

    def test_no_observed_candidate_does_not_scan_session_messages(self):
        self.run_scenario("no_candidate")

    def test_native_compaction_continuation_preserves_actual_parent_and_consumes_witness(self):
        self.run_scenario("compaction_continuation")

    def test_synthetic_parent_requires_complete_exclusive_scoped_parts(self):
        self.run_scenario("compaction_invalid_parent")

    def test_incomplete_lifecycle_or_old_event_metadata_cannot_bypass_verification(self):
        self.run_scenario("compaction_missing_witness")

    def test_native_marker_and_synthetic_events_never_register_human_turns(self):
        self.run_scenario("compaction_native_user_events")

    def test_new_user_or_deletion_during_exact_reads_invalidates_continuation(self):
        self.run_scenario("compaction_new_user_races")

    def test_fresh_instance_classifies_initial_event_only_user(self):
        self.run_scenario("fresh_instance_initial_user")

    def test_marker_event_before_parts_and_compacting_preserves_original_human(self):
        self.run_scenario("early_compaction_marker")

    def test_unknown_early_event_and_new_chat_cannot_register_old_human(self):
        self.run_scenario("early_unknown_and_new_chat")

    def test_second_plugin_instance_cannot_register_native_synthetic_user(self):
        self.run_scenario("two_plugin_instances")

    def test_queued_user_checks_block_completion_and_preserve_human_event_order(self):
        self.run_scenario("compaction_queued_user_checks")

    def test_late_verified_native_user_events_cannot_replace_new_human(self):
        self.run_scenario("compaction_user_replay")

    def test_unknown_user_invalidates_witness_and_augmented_human_registers(self):
        self.run_scenario("compaction_unknown_user")

    def test_summary_receipt_stays_separate_from_continued_answer(self):
        self.run_scenario("compaction_summary_separate")

    def test_concurrent_user_registrations_keep_durable_observed_order(self):
        self.run_scenario("user_registration_order")


if __name__ == "__main__":
    unittest.main()
