import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { AddressInfo } from "node:net";
import { createApp } from "../src/app.ts";
import { Store } from "../src/store.ts";
import { messages } from "../web/i18n.js";

const TOKEN = "t".repeat(40);
const WEBSITE = "https://www.example.com";

async function boot(options: { proxyHops?: number } = {}) {
  const service = createApp({
    dbPath: ":memory:",
    origin: "https://consult.example.com",
    controlToken: TOKEN,
    ...options,
  });
  const server = service.app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  let polling = true;
  const call = async (
    path: string,
    init: { method?: string; body?: unknown; headers?: Record<string, string> } = {},
  ) => {
    const response = await fetch(base + path, {
      method: init.method ?? (init.body === undefined ? "GET" : "POST"),
      headers: { "Content-Type": "application/json", ...init.headers },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
    });
    const text = await response.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status: response.status, body };
  };
  const admin = (path: string, method = "GET", body?: unknown) =>
    call(path, { method, body, headers: { Authorization: "Bearer " + TOKEN } });
  // A stand-in for the model connector: answers every question with a fixed text.
  async function connect(siteId: string, capabilities: Record<string, boolean> = { hermes: true }) {
    const { body } = await admin(`/api/sites/${siteId}/pair`, "POST", {});
    const paired = await call("/connector/pair", { body: { code: body.code } });
    const auth = { Authorization: "Bearer " + paired.body.token };
    await call("/connector/heartbeat", { body: { capabilities }, headers: auth });
    void (async () => {
      while (polling) {
        const job = await call("/connector/jobs", { headers: auth }).catch(() => null);
        if (!job || job.status !== 200 || job.body.idle) continue;
        const data = job.body.kind === "ask" ? { text: "answer" } : { ok: true };
        await call("/connector/result", { body: { id: job.body.id, ok: true, data }, headers: auth });
      }
    })();
  }
  async function site(config: Record<string, unknown> = {}) {
    const created = await admin("/api/sites", "POST", {
      key: "main",
      name: "Shop",
      origins: [WEBSITE],
      ...config,
    });
    assert.equal(created.status, 201, JSON.stringify(created.body));
    assert.equal((await admin(`/api/sites/${created.body.id}/publish`, "POST", {})).status, 200);
    return created.body;
  }
  const visit = (siteId: string, headers: Record<string, string> = {}, visitorId = crypto.randomUUID()) =>
    call(`/public/sites/${siteId}/sessions`, {
      body: { visitorId },
      headers: { Origin: WEBSITE, ...headers },
    });
  const ask = (token: string, text: string) =>
    call("/public/session/ask", {
      body: { text, requestId: crypto.randomUUID() },
      headers: { Authorization: "Bearer " + token },
    });
  return {
    call, admin, connect, site, visit, ask,
    close() {
      polling = false;
      service.close();
      server.closeAllConnections();
      server.close();
    },
  };
}

test("refuses to start without a strong control token", () => {
  for (const controlToken of ["", "short"])
    assert.throws(() => createApp({ dbPath: ":memory:", origin: "https://x.example", controlToken }));
});

test("control API needs the token and refuses proxied requests even with it", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  assert.equal((await app.call("/api/sites")).status, 403);
  assert.equal((await app.call("/api/sites", { headers: { Authorization: "Bearer wrong" } })).status, 403);
  assert.equal((await app.admin("/api/sites")).status, 200);
  for (const header of ["X-Forwarded-For", "Forwarded", "Via"]) {
    const proxied = { Authorization: "Bearer " + TOKEN, [header]: "1.2.3.4" };
    assert.equal((await app.call("/api/sites", { headers: proxied })).status, 404, header);
    assert.equal((await app.call("/connector/jobs", { headers: proxied })).status, 404, header);
  }
});

test("the web admin and account endpoints are gone", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  for (const path of ["/api/register", "/api/login"])
    assert.equal((await app.call(path, { body: { email: "a@b.co", password: "x".repeat(12) } })).status, 403);
  for (const path of ["/", "/index.html", "/admin.js", "/demo.html", "/style.css"])
    assert.equal((await app.call(path)).status, 404, path);
  assert.equal((await app.call("/widget.js")).status, 200);
});

test("caps default on, cannot be turned off, and unknown settings are refused", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  const created = await app.site();
  assert.deepEqual(
    {
      engine: created.draft.engine,
      maxSeconds: created.draft.maxSeconds,
      maxConcurrent: created.draft.maxConcurrent,
      dailySeconds: created.draft.dailySeconds,
      dailyStarts: created.draft.dailyStarts,
      cooldownSeconds: created.draft.cooldownSeconds,
      sessionQuestions: created.draft.sessionQuestions,
      dailyQuestions: created.draft.dailyQuestions,
      dailyLeads: created.draft.dailyLeads,
    },
    {
      engine: "text", maxSeconds: 180, maxConcurrent: 1, dailySeconds: 1800, dailyStarts: 3,
      cooldownSeconds: 60, sessionQuestions: 30, dailyQuestions: 200, dailyLeads: 50,
    },
  );
  const base = { key: "b", name: "B", origins: [WEBSITE] };
  for (const bad of [
    { dailyQuestions: 0 },
    { sessionQuestions: 0 },
    { dailyLeads: 0 },
    { dailySeconds: 0 },
    { engine: "subscription" },
    { dailyQuestion: 5 },
    { key: "Bad Key" },
  ])
    assert.equal((await app.admin("/api/sites", "POST", { ...base, ...bad })).status, 400, JSON.stringify(bad));
});

test("a text-only site never offers voice, even when the connector could", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  const created = await app.site();
  await app.connect(created.id, { hermes: true, api: true });
  const info = await app.call(`/public/sites/${created.id}`);
  assert.equal(info.body.engine, "text");
  assert.equal(info.body.voiceReady, false);
  const session = await app.visit(created.id);
  assert.equal(session.status, 201);
  const start = await app.call("/public/session/start", {
    body: { offer: "v=0 offer sdp" },
    headers: { Authorization: "Bearer " + session.body.token },
  });
  assert.equal(start.status, 503);
  assert.equal(start.body.code, "errVoiceNotReady");
});

test("questions are capped per session and per site per day", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  const created = await app.site({ sessionQuestions: 2, dailyQuestions: 3, cooldownSeconds: 0, dailyStarts: 10 });
  await app.connect(created.id);
  const first = (await app.visit(created.id)).body.token;
  assert.equal((await app.ask(first, "one")).status, 200);
  assert.equal((await app.ask(first, "two")).status, 200);
  const third = await app.ask(first, "three");
  assert.equal(third.status, 429);
  assert.equal(third.body.code, "errQuestionLimit");
  await app.call("/public/session/end", { body: {}, headers: { Authorization: "Bearer " + first } });
  const second = (await app.visit(created.id)).body.token;
  assert.equal((await app.ask(second, "three")).status, 200);
  const over = await app.ask(second, "four");
  assert.equal(over.status, 429);
  assert.equal(over.body.code, "errDailyQuestions");
});

test("leads are capped per day and handed off exactly once", async (t) => {
  const app = await boot();
  t.after(() => app.close());
  const created = await app.site({ dailyLeads: 1 });
  const lead = { name: "Ada", contact: "ada@example.com", message: "Call me", consent: true };
  const post = (headers: Record<string, string>) =>
    app.call(`/public/sites/${created.id}/leads`, { body: lead, headers });
  assert.equal((await post({ Origin: "https://evil.example" })).status, 403);
  assert.equal((await post({ Origin: WEBSITE })).status, 201);
  const capped = await post({ Origin: WEBSITE });
  assert.equal(capped.status, 429);
  assert.equal(capped.body.code, "errDailyLeads");
  assert.equal((await app.call("/api/leads/pending")).status, 403);
  const pending = await app.admin("/api/leads/pending");
  assert.equal(pending.body.length, 1);
  assert.equal(pending.body[0].siteKey, "main");
  assert.equal(pending.body[0].siteName, "Shop");
  assert.equal(pending.body[0].name, "Ada");
  const id = pending.body[0].id;
  assert.equal((await app.admin(`/api/leads/${id}/handed`, "POST", { ref: "kanban:t_1" })).status, 200);
  assert.equal((await app.admin(`/api/leads/${id}/handed`, "POST", { ref: "kanban:t_1" })).status, 404);
  assert.equal((await app.admin("/api/leads/pending")).body.length, 0);
  const activity = await app.admin(`/api/sites/${created.id}/activity`);
  assert.equal(activity.body.leads[0].ref, "kanban:t_1");
});

test("behind a trusted proxy, per-visitor caps use the forwarded address", async (t) => {
  const app = await boot({ proxyHops: 1 });
  t.after(() => app.close());
  const created = await app.site({ dailyStarts: 1, cooldownSeconds: 0, maxConcurrent: 5 });
  await app.connect(created.id);
  assert.equal((await app.visit(created.id, { "X-Forwarded-For": "198.51.100.1" })).status, 201);
  const again = await app.visit(created.id, { "X-Forwarded-For": "198.51.100.1" });
  assert.equal(again.body.code, "errDailyVisitor");
  assert.equal((await app.visit(created.id, { "X-Forwarded-For": "198.51.100.2" })).status, 201);
});

test("an older database gains the lead handoff columns", () => {
  const path = join(mkdtempSync(join(tmpdir(), "consult-")), "old.sqlite");
  const old = new DatabaseSync(path);
  old.exec(
    "CREATE TABLE leads(id TEXT PRIMARY KEY,session TEXT UNIQUE NOT NULL,site TEXT NOT NULL,name TEXT NOT NULL,contact TEXT NOT NULL,message TEXT NOT NULL,created INTEGER NOT NULL);" +
      "INSERT INTO leads VALUES ('l1','l1','s','n','c','m',1);",
  );
  old.close();
  const store = new Store(path);
  assert.deepEqual({ ...store.get("SELECT handed,ref FROM leads WHERE id='l1'") }, { handed: 0, ref: null });
  store.close();
});

test("every error code the relay sends has English and Korean text", () => {
  const source = readFileSync(new URL("../src/app.ts", import.meta.url), "utf8");
  const codes = [...source.matchAll(/"(err[A-Z]\w+)"/g)].map((m) => m[1]);
  assert.ok(codes.length > 20);
  for (const code of codes) {
    assert.ok(code in messages.en, code);
    assert.ok(code in messages.ko, code);
  }
});

test("visitor-facing files and text name no engine or vendor", () => {
  const banned = /hermes|codex|openai|chatgpt|gpt-|subscription|desktop|claude|anthropic|헤르메스/i;
  const web = new URL("../web/", import.meta.url);
  for (const file of readdirSync(web))
    assert.doesNotMatch(readFileSync(new URL(file, web), "utf8"), banned, file);
  for (const locale of ["en", "ko"] as const)
    for (const [key, value] of Object.entries(messages[locale]))
      assert.doesNotMatch(String(value), banned, `${locale}.${key}`);
});
