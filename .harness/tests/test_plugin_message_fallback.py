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
const bridge = [], calls = [], logs = [];
let delayFirstUser = false, releaseFirstUser, durableUser = "";
globalThis.Bun = { spawn: (args, options) => {
  bridge.push(args.slice(2));
  assert.equal(options.stdin, "ignore", "These hooks must send no private payload");
  let exited = Promise.resolve(0);
  if (args[2] === "user-turn") {
    const id = args[args.indexOf("--message-id") + 1];
    if (delayFirstUser && id === "u1") exited = new Promise(resolve => { releaseFirstUser = () => {durableUser = id; resolve(0);}; });
    else durableUser = id;
  }
  if (args[2] === "agent-turn-terminal" && !args.includes("--is-summary"))
    assert.equal(args[args.indexOf(args.includes("--compaction-continuation-of") ? "--compaction-continuation-of" : "--parent-message-id") + 1], durableUser, "Terminal must follow the latest durable user registration");
  return {stdout: new Response("{}").body, stderr: new Response("").body, exited};
}};
let lookup = async () => { throw new Error("Unexpected exact lookup"); };
let active = 0, maxActive = 0;
const { AwokiContinuity } = await import(pathToFileURL(pluginPath).href);
const client = {
  app: {log: async value => logs.push(value)},
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

if (scenario === "session_metadata_after_idle") {
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
