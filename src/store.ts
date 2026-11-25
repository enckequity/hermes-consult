import { DatabaseSync } from "node:sqlite";
import { randomBytes, createHash } from "node:crypto";
import { mkdirSync, chmodSync } from "node:fs";
import { dirname } from "node:path";
export const secret = () => randomBytes(32).toString("base64url");
export const hash = (v: string) => createHash("sha256").update(v).digest("hex");
export class Store {
  db: DatabaseSync;
  constructor(path: string) {
    if (path !== ":memory:")
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    if (path !== ":memory:") chmodSync(path, 0o600);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
   CREATE TABLE IF NOT EXISTS starts(site TEXT NOT NULL,identity TEXT NOT NULL,started INTEGER NOT NULL);
   CREATE INDEX IF NOT EXISTS starts_identity ON starts(site,identity,started);
   CREATE TABLE IF NOT EXISTS questions(site TEXT NOT NULL,at INTEGER NOT NULL);
   CREATE INDEX IF NOT EXISTS questions_site ON questions(site,at);
   CREATE TABLE IF NOT EXISTS sites(id TEXT PRIMARY KEY,owner TEXT NOT NULL,draft TEXT NOT NULL,published TEXT,revision INTEGER NOT NULL DEFAULT 0);
   CREATE TABLE IF NOT EXISTS consultations(id TEXT PRIMARY KEY,site TEXT NOT NULL,started INTEGER NOT NULL,ended INTEGER,status TEXT NOT NULL,reserved INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL,engine TEXT NOT NULL);
   CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY,session TEXT UNIQUE NOT NULL,site TEXT NOT NULL,name TEXT NOT NULL,contact TEXT NOT NULL,message TEXT NOT NULL,created INTEGER NOT NULL,handed INTEGER NOT NULL DEFAULT 0,ref TEXT);
  `);
    // Databases created before the lead handoff lack its columns.
    const columns = this.all("PRAGMA table_info(leads)").map((c) => c.name);
    // Upstream rows: sites move to the single local owner so the service manages them, and
    // leads collected before the handoff existed are not filed as new cards.
    if (!columns.includes("handed"))
      this.db.exec(
        "ALTER TABLE leads ADD COLUMN handed INTEGER NOT NULL DEFAULT 0; ALTER TABLE leads ADD COLUMN ref TEXT;" +
          "UPDATE leads SET handed=1,ref='before-handoff'; UPDATE sites SET owner='local';" +
          // Sites made outside sites.json have no key: they stay stored but off until listed.
          "UPDATE sites SET published=NULL WHERE json_extract(draft,'$.key') IS NULL;",
      );
    // Process restart invalidates in-memory tokens; keep reserved budget for unknown upstream closure.
    this.db
      .prepare(
        "UPDATE consultations SET status='interrupted',ended=? WHERE status='active'",
      )
      .run(Date.now());
  }
  get(sql: string, ...args: any[]) {
    return this.db.prepare(sql).get(...args) as any;
  }
  all(sql: string, ...args: any[]) {
    return this.db.prepare(sql).all(...args) as any[];
  }
  run(sql: string, ...args: any[]) {
    return this.db.prepare(sql).run(...args);
  }
  close() {
    this.db.close();
  }
}
