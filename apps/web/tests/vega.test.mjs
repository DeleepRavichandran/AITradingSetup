// Unit tests for the Vega command parser: node --test tests/  (Node 22 strips TypeScript types on import)
import assert from "node:assert/strict";
import test from "node:test";
import { OPERATIONAL, parse } from "../lib/vega.ts";

const kind = (s, c) => parse(s, c).kind;

test("exact operational phrases are recognised", () => {
  for (const s of ["engage kill switch", "Engage the kill switch.", "Hey Vega, stop all trading", "emergency stop", "halt everything", "please activate the emergency stop"])
    assert.equal(kind(s), "kill", s);
  assert.equal(kind("run replay"), "replay");
  assert.equal(kind("start a five session replay"), "replay");
  assert.equal(kind("run research"), "research");
  for (const k of ["kill", "replay", "research"]) assert.ok(OPERATIONAL.has(k));
});

test("ambiguous, negated or questioning speech never triggers an action", () => {
  for (const s of ["don't stop trading", "do not engage the kill switch", "never run research", "I said not the kill switch",
                   "kill", "stop", "the kill switch thing", "trading is fun", "replay it later maybe", "research stuff"])
    assert.notEqual(["kill", "replay", "research"].includes(kind(s)), true, s);
  for (const s of ["what is the kill switch", "how does replay work", "should I stop trading", "explain the research loop"])
    assert.equal(kind(s), "ask", s);
  assert.equal(kind("stop"), "cancel");
  assert.match(parse("stop trading maybe").reason ?? "", /engage kill switch/);
});

test("low recognition confidence never triggers an operational action", () => {
  assert.equal(kind("engage kill switch", 0.42), "clarify");
  assert.equal(kind("engage kill switch", 0.91), "kill");
  assert.equal(kind("status report", 0.3), "status");          // read-only intents are fine
});

test("safety resets and orders are always refused", () => {
  for (const s of ["reset the kill switch", "turn off the kill switch", "resume trading", "disable the loss limit", "override safety", "clear emergency halt"])
    assert.equal(kind(s), "refuse_reset", s);
  for (const s of ["buy 100 reliance", "sell infosys", "place an order for tcs", "square off everything", "go long nifty"])
    assert.equal(kind(s), "refuse_order", s);
  assert.equal(kind("reset the kill switch", 0.99), "refuse_reset");
});

test("confirmations, read-only intents and navigation", () => {
  assert.equal(kind("confirm"), "confirm"); assert.equal(kind("yes do it"), "confirm");
  assert.equal(kind("cancel"), "cancel"); assert.equal(kind("never mind"), "cancel");
  assert.equal(kind("give me a status report"), "status");
  assert.equal(kind("what's the world news"), "news");
  assert.equal(kind("portfolio"), "portfolio");
  assert.deepEqual(parse("open strategy monitor"), { kind: "open", path: "/screening", name: "Strategy Monitor" });
  assert.equal(kind("open the moon"), "clarify");
  assert.equal(kind("why did nifty fall today"), "ask");
  assert.equal(kind("   "), "empty");
});
