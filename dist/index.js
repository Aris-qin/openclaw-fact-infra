// src/index.ts
import { createRequire } from "node:module";

// src/schemas.ts
var FactError = class extends Error {
  constructor(tag, message = tag, details = {}, options) {
    super(message, options);
    this.tag = tag;
    this.details = details;
  }
};
var fail = (m) => {
  throw new FactError("VALIDATION_ERROR", m);
};
var cpCompare = (a, b) => {
  const x = Array.from(a, (c) => c.codePointAt(0)), y = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
  return x.length - y.length;
};
function validString(s) {
  for (const c of s) {
    const n = c.codePointAt(0);
    if (n >= 55296 && n <= 57343) fail("lone surrogate");
  }
}
function serialize(v, spaces, ascii = false) {
  const quote = (s) => {
    let q = JSON.stringify(s);
    if (ascii) q = q.replace(/[\u007f-\uffff]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
    return q;
  };
  if (v === null || typeof v === "boolean" || typeof v === "number") return JSON.stringify(v);
  if (typeof v === "string") return quote(v);
  if (Array.isArray(v)) return "[" + v.map((x) => serialize(x, spaces, ascii)).join(spaces ? ", " : ",") + "]";
  return "{" + Object.keys(v).sort(cpCompare).map((k) => quote(k) + (spaces ? ": " : ":") + serialize(v[k], spaces, ascii)).join(spaces ? ", " : ",") + "}";
}
function validateJson(value, bounded2) {
  let nodes = 0;
  const active = /* @__PURE__ */ new Set();
  function visit(v, d) {
    if (bounded2 && (d > 12 || ++nodes > 5e3)) fail("JSON depth/node limit");
    if (v === null || typeof v === "boolean") return;
    if (typeof v === "string") {
      validString(v);
      if (bounded2 && Array.from(v).length > 2e5) fail("string limit");
      return;
    }
    if (typeof v === "number") {
      if (!Number.isFinite(v) || Number.isInteger(v) && !Number.isSafeInteger(v)) fail("number outside supported domain");
      return;
    }
    if (typeof v !== "object" || !v || !Array.isArray(v) && Object.getPrototypeOf(v) !== Object.prototype) return fail("ordinary JSON required");
    if (active.has(v)) fail("cyclic JSON");
    active.add(v);
    if (Array.isArray(v)) {
      for (let i = 0; i < v.length; i++) {
        if (!Object.hasOwn(v, i)) fail("sparse array");
        visit(v[i], d + 1);
      }
    } else {
      if (Reflect.ownKeys(v).length !== Object.keys(v).length) fail("non JSON properties");
      for (const [k, x] of Object.entries(v)) {
        validString(k);
        visit(x, d + 1);
      }
    }
    active.delete(v);
  }
  visit(value, 0);
  if (bounded2 && Buffer.byteLength(serialize(value, true), "utf8") > 1e6) fail("JSON byte limit");
}
function validateJsonBounds(value) {
  validateJson(value, true);
}
function internalJson(value, spaces = false) {
  validateJson(value, false);
  return serialize(value, spaces);
}
function canonicalJson(value) {
  return internalJson(value);
}
var keywords = new Set("type properties additionalProperties required enum const minLength maxLength minimum maximum minItems maxItems maxProperties items allOf if then not anyOf oneOf".split(" "));
function checkSchema(s) {
  for (const k of Object.keys(s)) if (!keywords.has(k)) throw new Error("Unsupported schema keyword: " + k);
  for (const q of Object.values(s.properties ?? {})) checkSchema(q);
  for (const k of ["items", "if", "then", "not"]) if (s[k]) checkSchema(s[k]);
  if (typeof s.additionalProperties === "object") checkSchema(s.additionalProperties);
  for (const k of ["allOf", "anyOf", "oneOf"]) for (const q of s[k] ?? []) checkSchema(q);
}
function validateSchema(value, schema, path4 = "$") {
  checkSchema(schema);
  function test(v, s, p) {
    const bad3 = (m) => fail(p + ": " + m);
    const matches = (q) => {
      try {
        test(v, q, p);
        return true;
      } catch (e) {
        if (e instanceof FactError) return false;
        throw e;
      }
    };
    if (s.type) {
      const ts = Array.isArray(s.type) ? s.type : [s.type];
      if (!ts.some((t) => t === "null" ? v === null : t === "array" ? Array.isArray(v) : t === "object" ? v !== null && typeof v === "object" && !Array.isArray(v) : t === "integer" ? typeof v === "number" && Number.isSafeInteger(v) : t === "number" ? typeof v === "number" && Number.isFinite(v) : typeof v === t)) bad3("type");
    }
    if (s.enum && !s.enum.some((x) => JSON.stringify(x) === JSON.stringify(v))) bad3("enum");
    if (Object.hasOwn(s, "const") && JSON.stringify(s.const) !== JSON.stringify(v)) bad3("const");
    if (typeof v === "string") {
      const n = Array.from(v).length;
      if (n < (s.minLength ?? 0) || n > (s.maxLength ?? Infinity)) bad3("string length");
    }
    if (typeof v === "number" && (v < (s.minimum ?? -Infinity) || v > (s.maximum ?? Infinity))) bad3("number range");
    if (Array.isArray(v)) {
      if (v.length < (s.minItems ?? 0) || v.length > (s.maxItems ?? Infinity)) bad3("array length");
      if (s.items) v.forEach((x, i) => test(x, s.items, p + "[" + i + "]"));
    }
    if (v !== null && typeof v === "object" && !Array.isArray(v)) {
      for (const k of s.required ?? []) if (!Object.hasOwn(v, k)) bad3("required " + k);
      if (Object.keys(v).length > (s.maxProperties ?? Infinity)) bad3("property count");
      for (const [k, x] of Object.entries(v)) {
        if (Object.hasOwn(s.properties ?? {}, k)) test(x, s.properties[k], p + "." + k);
        else if (s.additionalProperties === false) bad3(k === "item_ref" ? "item_ref removed; use item_id" : "unknown field " + k);
        else if (typeof s.additionalProperties === "object") test(x, s.additionalProperties, p + "." + k);
      }
    }
    for (const q of s.allOf ?? []) test(v, q, p);
    if (s.if && matches(s.if) && s.then) test(v, s.then, p);
    if (s.not && matches(s.not)) bad3("forbidden fields");
    if (s.anyOf && !s.anyOf.some(matches)) bad3("anyOf");
    if (s.oneOf && s.oneOf.filter(matches).length !== 1) bad3("oneOf");
  }
  test(value, schema, path4);
}
var str = (max = 1e4, min = 1) => ({ type: "string", minLength: min, maxLength: max });
var en = (...xs) => ({ type: "string", enum: xs });
var obj = (properties, required = []) => ({ type: "object", properties, required, additionalProperties: false });
var arr = (items, min = 0) => ({ type: "array", items, minItems: min, maxItems: 100 });
var strings = (keys) => Object.fromEntries(keys.split(" ").map((k) => [k, str()]));
var link = obj({ item_id: str(), relation: en("context", "affects", "evidence") });
var relation = obj({ target_id: str(), target_ref: str(), relation: en("supports", "contradicts", "evidence_for", "follows") });
var file = obj({ ...strings("locator purpose corpus_id source_locator item_id"), storage: en("local", "corpus", "external"), role: en("temp", "artifact", "reference"), handoff_status: en("not_applicable", "pending_handoff", "accepted", "failed") }, ["locator", "storage", "role", "purpose"]);
var shared = { links: arr(link), relations: arr(relation), files: arr(file) };
var record = obj({ ...strings("kind title body ref status supersedes_id supersedes_ref hypothesis workaround resolution issue_key summary rationale decision_key expected_result actual_result run_at result evidence"), ...shared, severity: en("low", "medium", "high", "critical"), verdict: en("pass", "fail", "inconclusive"), input_config: { type: ["object", "string"] } }, ["kind", "title", "body"]);
var change = obj({ ...strings("op ref name purpose parent_id parent_ref item_id item_ref requires_id requires_ref title description"), kind: en("goal", "task", "project"), position: { type: "integer", minimum: 0 }, acknowledge_migration_review: { type: "boolean" } }, ["op"]);
function tool(properties, rules) {
  return { ...obj({ action: en(...rules.map((r) => r[0])), ...properties }, ["action"]), allOf: rules.map(([action, required, forbidden]) => ({ if: { properties: { action: { const: action } }, required: ["action"] }, then: { required, ...forbidden.length ? { not: { anyOf: forbidden.map((k) => ({ required: [k] })) } } : {} } })) };
}
var ids = Object.fromEntries("work request_id proposal_id id cursor record_id item_id target_id".split(" ").map((k) => [k, str(256)]));
var confirmation = obj({ summary: str(2e3) }, ["summary"]);
var schemas = {
  fact_create: tool({ slug: str(100), purpose: str(), kind: en("project", "light"), request_id: ids.request_id, name: str(500), root_path: str(4096) }, [["create", ["slug", "purpose", "kind", "request_id"], []]]),
  fact_read: tool({ work: ids.work, id: ids.id, cursor: ids.cursor, collection: en("items", "records", "files", "proposals"), limit: { type: "integer", minimum: 1, maximum: 200 } }, [["overview", [], ["work", "id", "collection", "limit", "cursor"]], ["current", ["work"], ["id", "collection", "cursor"]], ["detail", ["work"], []], ["history", ["work"], ["id", "collection"]]]),
  fact_plan: tool({ work: ids.work, request_id: ids.request_id, proposal_id: ids.proposal_id, confirmation, changes: arr(change, 1), review_summary: str() }, [["propose", ["work", "request_id", "changes"], ["proposal_id", "confirmation"]], ["apply", ["work", "request_id", "proposal_id", "confirmation"], ["changes", "review_summary"]], ["reject", ["work", "request_id", "proposal_id"], ["changes", "review_summary"]]]),
  fact_record: tool({ work: ids.work, request_id: ids.request_id, record_id: ids.record_id, item_id: ids.item_id, records: arr(record, 1), ...strings("title status resolution reason"), body: str(2e5, 0), payload: { type: "object", maxProperties: 100 }, ...shared, expected_version: { type: "integer", minimum: 1 } }, [["append", ["work", "request_id", "records"], "record_id item_id title body payload links relations files status resolution reason expected_version".split(" ")], ["update", ["work", "request_id", "record_id", "expected_version"], ["records", "item_id", "reason"]], ...["start", "block"].map((a) => [a, ["work", "request_id", "item_id", "expected_version"], "records record_id title body payload links relations files status resolution".split(" ")])]),
  fact_finish: tool({ work: ids.work, request_id: ids.request_id, proposal_id: ids.proposal_id, target_id: ids.target_id, target_type: en("work", "goal", "task"), mode: en("complete", "archive", "restore"), confirmation }, [["prepare", ["work", "request_id", "target_type", "target_id"], ["proposal_id", "confirmation"]], ["confirm", ["work", "request_id", "proposal_id", "confirmation"], ["mode", "target_type", "target_id"]], ["continue", ["work", "request_id", "proposal_id"], ["mode", "target_type", "target_id", "confirmation"]]])
};
schemas.fact_read.allOf.push({ if: { properties: { action: { const: "detail" } }, required: ["action"] }, then: { oneOf: [{ required: ["id"] }, { required: ["collection"] }] } });
for (const s of Object.values(schemas)) checkSchema(s);
function toolSchemas() {
  return schemas;
}
function validatePublic(name, raw) {
  validateJsonBounds(raw);
  validateSchema(raw, schemas[name]);
  const a = raw;
  if (name === "fact_read" && a.action === "detail" && a.id && ("limit" in a || "cursor" in a)) fail("entity detail forbids limit/cursor");
  if (name === "fact_create" && !/^[a-z0-9_-]+$/.test(a.slug)) fail("invalid slug");
  return a;
}
var configSchema = obj({ workRoot: str(4096), indexPath: str(4096), staleDays: { type: "integer", minimum: 0 }, sqliteTimeoutMs: { type: "integer", minimum: 0 } }, ["workRoot", "indexPath"]);
function parseConfig(value) {
  validateJsonBounds(value);
  validateSchema(value, configSchema);
  const v = value;
  return { ...v, projectsRoot: v.workRoot + "/projects", workDir: v.workRoot + "/work", staleDays: v.staleDays ?? 14, sqliteTimeoutMs: v.sqliteTimeoutMs ?? 3e4 };
}

// src/completion.ts
import { randomUUID } from "node:crypto";

// src/store.ts
import { DatabaseSync } from "node:sqlite";
import { existsSync } from "node:fs";
var DDL = "\nCREATE TABLE work(id TEXT PRIMARY KEY,slug TEXT UNIQUE NOT NULL,name TEXT NOT NULL,purpose TEXT NOT NULL,\n kind TEXT NOT NULL CHECK(kind IN('project','light')),status TEXT NOT NULL DEFAULT 'active',revision INTEGER NOT NULL DEFAULT 0,\n root_items_version INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);\nCREATE TABLE items(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN('goal','task')),parent_id TEXT REFERENCES items(id),\n title TEXT NOT NULL,description TEXT,status TEXT NOT NULL DEFAULT 'planned',position INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,\n children_version INTEGER NOT NULL DEFAULT 0,completed_at TEXT,removed_at TEXT,legacy_payload TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);\nCREATE TABLE dependencies(item_id TEXT REFERENCES items(id),requires_id TEXT REFERENCES items(id),PRIMARY KEY(item_id,requires_id));\nCREATE TABLE records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,\n issue_key TEXT UNIQUE,decision_key TEXT UNIQUE,supersedes_id TEXT REFERENCES records(id),version INTEGER NOT NULL DEFAULT 1,\n legacy_payload TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);\nCREATE TABLE record_links(record_id TEXT REFERENCES records(id),item_id TEXT REFERENCES items(id),relation TEXT NOT NULL,\n UNIQUE(record_id,item_id,relation));\nCREATE UNIQUE INDEX record_work_link ON record_links(record_id,relation) WHERE item_id IS NULL;\nCREATE TABLE record_relations(source_id TEXT REFERENCES records(id),target_id TEXT REFERENCES records(id),relation TEXT NOT NULL,\n PRIMARY KEY(source_id,target_id,relation));\nCREATE TABLE file_refs(id TEXT PRIMARY KEY,record_id TEXT REFERENCES records(id),item_id TEXT REFERENCES items(id),locator TEXT NOT NULL,\n storage TEXT NOT NULL,role TEXT NOT NULL,purpose TEXT NOT NULL,handoff_status TEXT NOT NULL DEFAULT 'not_applicable',corpus_id TEXT,\n source_locator TEXT,created_at TEXT NOT NULL);\nCREATE TABLE proposals(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload_json TEXT NOT NULL,base_versions_json TEXT NOT NULL,\n review_summary TEXT,status TEXT NOT NULL,source_session TEXT,confirmation_json TEXT,created_at TEXT NOT NULL,applied_at TEXT);\nCREATE TABLE history(seq INTEGER PRIMARY KEY AUTOINCREMENT,operation TEXT NOT NULL,target_type TEXT,target_id TEXT,before_json TEXT,\n after_json TEXT,request_id TEXT,proposal_id TEXT,source_session TEXT,occurred_at TEXT NOT NULL);\nCREATE TRIGGER history_no_update BEFORE UPDATE ON history BEGIN SELECT RAISE(ABORT,'history is append-only'); END;\nCREATE TRIGGER history_no_delete BEFORE DELETE ON history BEGIN SELECT RAISE(ABORT,'history is append-only'); END;\nCREATE TABLE request_receipts(request_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,response_json TEXT NOT NULL,committed_revision INTEGER NOT NULL);\nCREATE TABLE legacy_map(old_table TEXT,old_id TEXT,old_key TEXT,new_type TEXT,new_id TEXT,PRIMARY KEY(old_table,old_id));\nCREATE TABLE legacy_meta(key TEXT PRIMARY KEY,value TEXT);\n";
function connect(path4, write, timeoutMs, gate = false) {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 0) throw new FactError("VALIDATION_ERROR", "invalid timeout");
  const db = new DatabaseSync(path4, { readOnly: !write });
  try {
    if (gate) {
      const v = Number(db.prepare("PRAGMA user_version").get().user_version);
      if (v !== 3) throw new FactError(v < 3 ? "MIGRATION_REQUIRED" : "UNSUPPORTED_SCHEMA");
    }
    db.exec(`PRAGMA foreign_keys=ON; PRAGMA busy_timeout=${timeoutMs}`);
    if (write) db.exec("PRAGMA journal_mode=WAL");
    return { db, path: path4, statementCache: /* @__PURE__ */ new Map(), closed: false };
  } catch (e) {
    db.close();
    throw e;
  }
}
function openStore(path4, options) {
  if (path4 !== ":memory:" && !existsSync(path4)) throw new FactError("VALIDATION_ERROR", "database does not exist");
  if (options.write && options.gate !== false) {
    const probe = connect(path4, false, options.timeoutMs, true);
    closeStore(probe);
  }
  return connect(path4, options.write, options.timeoutMs, options.gate !== false);
}
function openNewStore(path4, timeoutMs) {
  return connect(path4, true, timeoutMs);
}
function initializeSchema(s) {
  s.db.exec(DDL);
  s.db.exec("PRAGMA user_version=3");
}
function statement(s, sql) {
  let q = s.statementCache.get(sql);
  if (!q) {
    q = s.db.prepare(sql);
    q.setReadBigInts(true);
    s.statementCache.set(sql, q);
  }
  return q;
}
function row(r) {
  if (!r) return r;
  return Object.fromEntries(Object.entries(r).map(([k, v]) => {
    if (typeof v === "bigint") {
      const n = Number(v);
      if (!Number.isSafeInteger(n)) throw new FactError("VALIDATION_ERROR", "unsafe database integer");
      return [k, n];
    }
    return [k, v];
  }));
}
function getRow(s, sql, params = []) {
  return row(statement(s, sql).get(...params));
}
function allRows(s, sql, params = []) {
  return statement(s, sql).all(...params).map(row);
}
function run(s, sql, params = []) {
  return statement(s, sql).run(...params);
}
function closeStore(s) {
  if (!s.closed) {
    s.statementCache.clear();
    s.db.close();
    s.closed = true;
  }
}

// src/completion.ts
function relevantData(tx, type, id) {
  const ids2 = allRows(tx, type === "work" ? "SELECT id FROM items WHERE removed_at IS NULL ORDER BY id" : "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT i.id FROM items i JOIN tree t ON i.parent_id=t.id WHERE i.removed_at IS NULL) SELECT id FROM tree ORDER BY id", type === "work" ? [] : [id]).map((r) => r.id);
  const marks = (xs) => xs.length ? xs.map(() => "?").join(",") : "NULL";
  const records = type === "work" ? allRows(tx, "SELECT * FROM records ORDER BY id") : allRows(tx, `SELECT DISTINCT r.* FROM records r JOIN record_links l ON l.record_id=r.id WHERE l.item_id IN (${marks(ids2)}) ORDER BY r.id`, ids2);
  const rs = records.map((r) => r.id);
  return { work: getRow(tx, "SELECT id,status,kind FROM work"), items: allRows(tx, `SELECT * FROM items WHERE id IN (${marks(ids2)}) ORDER BY id`, ids2), dependencies: allRows(tx, `SELECT * FROM dependencies WHERE item_id IN (${marks(ids2)}) OR requires_id IN (${marks(ids2)}) ORDER BY item_id,requires_id`, [...ids2, ...ids2]), records, links: allRows(tx, `SELECT * FROM record_links WHERE record_id IN (${marks(rs)}) ORDER BY record_id,item_id,relation`, rs), relations: allRows(tx, `SELECT * FROM record_relations WHERE source_id IN (${marks(rs)}) OR target_id IN (${marks(rs)}) ORDER BY source_id,target_id,relation`, [...rs, ...rs]), files: allRows(tx, `SELECT * FROM file_refs WHERE record_id IN (${marks(rs)}) OR item_id IN (${marks(ids2)}) ORDER BY id`, [...rs, ...ids2]) };
}
function relevantDigest(tx, type, id) {
  return { digest: payloadHash(relevantData(tx, type, id)) };
}
function handleFinish(tx, a) {
  const prior = readReceipt(tx, a.request_id, a);
  if (prior) return prior;
  if (["confirm", "continue"].includes(a.action) && a.proposal_id) {
    const p = getRow(tx, "SELECT status FROM proposals WHERE id=?", [a.proposal_id]);
    if (p && ["applied", "rejected"].includes(p.status)) {
      const receipt = findTerminalReceipt(tx, a.proposal_id);
      if (receipt) return receipt;
    }
  }
  if (getRow(tx, "SELECT status FROM work").status === "imported_unreviewed") throw new FactError("ADOPTION_REQUIRED");
  const t = (/* @__PURE__ */ new Date()).toISOString();
  let response, pid;
  if (a.action === "prepare") {
    const w = getRow(tx, "SELECT * FROM work"), mode = a.mode ?? "complete";
    if (a.target_type === "work" && a.target_id !== w.id) throw new FactError("VALIDATION_ERROR", "target work mismatch");
    if (mode !== "complete" && a.target_type !== "work") throw new FactError("VALIDATION_ERROR", "archive/restore are work-only");
    if (w.status === "archived" && mode !== "restore") throw new FactError("WORK_ARCHIVED");
    if (mode === "restore" && w.status !== "archived") throw new FactError("INVALID_TRANSITION");
    let status = w.status;
    if (a.target_type !== "work") {
      const i = getRow(tx, "SELECT * FROM items WHERE id=? AND kind=?", [a.target_id, a.target_type]);
      if (!i) throw new FactError("NOT_FOUND");
      status = i.status;
      run(tx, "UPDATE items SET status='awaiting_confirmation',version=version+1,updated_at=? WHERE id=?", [t, i.id]);
    }
    const base = relevantDigest(tx, a.target_type, a.target_id);
    pid = randomUUID();
    const payload = { mode, target_type: a.target_type, target_id: a.target_id, prior_status: status, open_items: allRows(tx, "SELECT id,kind,title,status FROM items WHERE status NOT IN('done','cancelled') AND removed_at IS NULL"), open_issues: allRows(tx, "SELECT id,title,status FROM records WHERE kind='issue' AND status='open'") };
    run(tx, "INSERT INTO proposals VALUES(?,?,?,?,?,'pending',NULL,NULL,?,NULL)", [pid, "completion", canonicalJson(payload), canonicalJson(base), "completion summary", t]);
    response = { ok: true, data: { proposal_id: pid, summary: payload, needs_user_confirmation: true }, follow_up: ["Ask whether to finish now or add method, result, or notes."] };
  } else {
    const p = getRow(tx, "SELECT * FROM proposals WHERE id=? AND kind='completion'", [a.proposal_id]);
    if (!p) throw new FactError("NOT_FOUND");
    pid = p.id;
    const v = JSON.parse(p.payload_json);
    if (a.action === "confirm") {
      if (!a.confirmation?.summary) throw new FactError("CONFIRMATION_REQUIRED");
      if (p.status !== "pending" || canonicalJson(relevantDigest(tx, v.target_type, v.target_id)) !== p.base_versions_json) throw new FactError("STALE_PROPOSAL");
      if (v.target_type === "work") run(tx, "UPDATE work SET status=?,updated_at=?", [{ complete: "completed", archive: "archived", restore: "active" }[v.mode], t]);
      else run(tx, "UPDATE items SET status='done',completed_at=?,version=version+1,updated_at=? WHERE id=?", [t, t, v.target_id]);
      run(tx, "UPDATE proposals SET status='applied',confirmation_json=?,applied_at=? WHERE id=?", [canonicalJson(a.confirmation), t, pid]);
      response = { ok: true, data: { proposal_id: pid, status: "applied", mode: v.mode } };
    } else {
      if (["rejected", "applied"].includes(p.status)) throw new FactError("STALE_PROPOSAL");
      if (v.target_type !== "work") run(tx, "UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=? AND status='awaiting_confirmation'", [v.prior_status ?? "planned", t, v.target_id]);
      run(tx, "UPDATE proposals SET status='rejected',applied_at=? WHERE id=?", [t, pid]);
      response = { ok: true, data: { proposal_id: pid, status: "continued" } };
    }
  }
  return commitMutation(tx, { rawArgs: a, requestId: a.request_id, response, operation: "finish_" + a.action, targetType: a.target_type ?? "proposal", targetId: a.target_id ?? a.proposal_id, proposalId: pid, before: null, after: response.data });
}

// src/views.ts
import * as fs2 from "node:fs";
import * as path2 from "node:path";

// src/migrations.ts
function readLegacyInfo(tx) {
  const read = (key) => {
    const r = getRow(tx, "SELECT value FROM legacy_meta WHERE key=?", [key]);
    return r ? JSON.parse(r.value) : null;
  };
  return { legacy_registry: read("registry_snapshot"), migration_review: read("migration_review_report") };
}
function assertAdoption(tx, changes) {
  if (getRow(tx, "SELECT status FROM work").status === "imported_unreviewed" && (changes.length !== 1 || changes[0].op !== "adopt")) throw new FactError("ADOPTION_REQUIRED");
}
function applyAdoption(tx, c) {
  if (getRow(tx, "SELECT status FROM work").status !== "imported_unreviewed") throw new FactError("INVALID_TRANSITION");
  const info = readLegacyInfo(tx), report = info.migration_review;
  if (report && Object.values(report.counts ?? {}).reduce((n, v) => n + Number(v), 0) !== 0) {
    if (c.acknowledge_migration_review !== true) throw new FactError("MIGRATION_REVIEW_ACK_REQUIRED");
    report.acknowledged = true;
    run(tx, "UPDATE legacy_meta SET value=? WHERE key='migration_review_report'", [canonicalJson(report)]);
  }
  run(tx, "UPDATE work SET status=?,updated_at=?", [info.legacy_registry?.raw?.status === "archived" ? "archived" : "active", (/* @__PURE__ */ new Date()).toISOString()]);
}

// src/records.ts
import { randomUUID as randomUUID2 } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
var bad = (m) => {
  throw new FactError("VALIDATION_ERROR", m);
};
var fields = { issue: ["severity", "hypothesis", "workaround", "resolution", "issue_key"], decision: ["summary", "rationale", "decision_key"], experiment: ["input_config", "expected_result", "actual_result", "verdict", "run_at"], result: ["result", "evidence"], note: [] };
function validateFile(tx, f) {
  if (Object.keys(f).some((k) => !["locator", "storage", "role", "purpose", "handoff_status", "corpus_id", "source_locator", "item_id"].includes(k))) bad("invalid file fields");
  if (!["local", "corpus", "external"].includes(f.storage) || !["temp", "artifact", "reference"].includes(f.role) || !f.locator) bad("invalid file");
  const hs = f.handoff_status ?? "not_applicable";
  if (!["not_applicable", "pending_handoff", "accepted", "failed"].includes(hs)) bad("invalid handoff status");
  if (f.storage === "corpus" && (hs !== "accepted" || !f.corpus_id)) bad("corpus requires accepted handoff and real corpus_id");
  if (f.storage === "local") {
    if (path.isAbsolute(f.locator) || f.locator.split("/").includes("..")) bad("local file locator escapes work root");
    const root = fs.realpathSync(path.dirname(tx.path));
    let current = root;
    for (const part of f.locator.split("/")) {
      current = path.resolve(current, part);
      if ((() => {
        try {
          fs.lstatSync(current);
          return true;
        } catch {
          return false;
        }
      })()) {
        try {
          current = fs.realpathSync(current);
        } catch {
          bad("invalid symbolic link");
        }
      }
      const rel = path.relative(root, current);
      if (rel === ".." || rel.startsWith("../") || path.isAbsolute(rel)) bad("local file locator escapes work root");
    }
  }
  return hs;
}
function file2(tx, rid, f) {
  const hs = validateFile(tx, f);
  run(tx, "INSERT INTO file_refs VALUES(?,?,?,?,?,?,?,?,?,?,?)", [randomUUID2(), rid, f.item_id ?? null, f.locator, f.storage, f.role, f.purpose, hs, f.corpus_id ?? null, f.source_locator ?? null, (/* @__PURE__ */ new Date()).toISOString()]);
}
function links(tx, rid, ls) {
  for (const l of ls) {
    if ("item_ref" in l) bad("item_ref removed; use item_id");
    if (Object.keys(l).some((k) => !["item_id", "relation"].includes(k))) bad("invalid link fields");
    if (l.item_id && !getRow(tx, "SELECT id FROM items WHERE id=?", [l.item_id])) bad("invalid item link");
    const rel = l.relation ?? "context";
    if (!["context", "affects", "evidence"].includes(rel)) bad("invalid link relation");
    run(tx, "INSERT INTO record_links VALUES(?,?,?)", [rid, l.item_id ?? null, rel]);
  }
}
function relations(tx, rid, rs, refs = {}, update = false) {
  for (const r of rs) {
    if (Object.keys(r).some((k) => !(update ? ["target_id", "relation"] : ["target_id", "target_ref", "relation"]).includes(k))) bad("invalid relation fields");
    const target = r.target_id || refs[r.target_ref];
    if (!target || target === rid || !["supports", "contradicts", "evidence_for", "follows"].includes(r.relation)) bad("invalid record relation");
    run(tx, "INSERT INTO record_relations VALUES(?,?,?)", [rid, target, r.relation]);
  }
}
function canStart(tx, r) {
  if (r.status !== "planned" || r.removed_at) return false;
  const blocked = (id) => !!getRow(tx, "SELECT 1 FROM dependencies d LEFT JOIN items i ON i.id=d.requires_id WHERE d.item_id=? AND (i.status!='done' OR i.removed_at IS NOT NULL OR i.id IS NULL)", [id]);
  if (blocked(r.id)) return false;
  if (r.parent_id) {
    const p = getRow(tx, "SELECT * FROM items WHERE id=?", [r.parent_id]);
    if (!p || p.removed_at || ["blocked", "cancelled"].includes(p.status) || blocked(p.id)) return false;
  }
  return true;
}
function handleRecord(tx, a) {
  const prior = readReceipt(tx, a.request_id, a);
  if (prior) return prior;
  const w = guardWork(tx), before = entityState(tx), t = (/* @__PURE__ */ new Date()).toISOString();
  let data;
  if (a.action === "append") {
    const refs = {}, pending = a.records.map((r) => {
      if (!fields[r.kind]) bad("invalid record kind");
      const common = ["kind", "title", "body", "ref", "status", "links", "relations", "files", "supersedes_id", "supersedes_ref"];
      if (Object.keys(r).some((k) => !common.includes(k) && !fields[r.kind].includes(k))) bad("fields invalid for " + r.kind);
      if (!r.title || !r.body) bad("title/body required");
      if (r.kind === "issue" && (!["open", "resolved"].includes(r.status ?? "open") || r.status === "resolved" && !r.resolution)) bad("invalid issue status or resolution required");
      if (r.kind === "decision" && !["active", "superseded"].includes(r.status ?? "active")) bad("invalid decision status");
      const id = randomUUID2();
      if (r.ref) refs[r.ref] = id;
      return { id, r };
    });
    for (const { id, r } of pending) {
      const payload = Object.fromEntries(fields[r.kind].filter((k) => k in r && !["issue_key", "decision_key"].includes(k)).map((k) => [k, r[k]]));
      const supersedes = r.supersedes_id || refs[r.supersedes_ref] || null;
      if (supersedes) {
        const old = getRow(tx, "SELECT * FROM records WHERE id=?", [supersedes]);
        if (!old && !pending.some((x) => x.id === supersedes) || r.kind !== "decision" || old && (old.kind !== "decision" || old.status === "superseded")) bad("invalid decision supersession");
        run(tx, "UPDATE records SET status='superseded',version=version+1,updated_at=? WHERE id=?", [t, supersedes]);
      }
      run(tx, "INSERT INTO records(id,kind,title,body,payload_json,status,issue_key,decision_key,supersedes_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", [id, r.kind, r.title, r.body, canonicalJson(payload), r.status || (r.kind === "issue" ? "open" : "active"), r.issue_key ?? null, r.decision_key ?? null, supersedes, t, t]);
      links(tx, id, r.links?.length ? r.links : [{ relation: "context" }]);
      relations(tx, id, r.relations ?? [], refs);
      for (const f of r.files ?? []) file2(tx, id, f);
    }
    data = { refs, count: a.records.length };
  } else if (a.action === "update") {
    const r = getRow(tx, "SELECT * FROM records WHERE id=?", [a.record_id]);
    if (!r) throw new FactError("NOT_FOUND");
    if (a.expected_version !== r.version) throw new FactError("VERSION_CONFLICT");
    const status = a.status ?? r.status, payload = "payload" in a ? { ...a.payload } : JSON.parse(r.payload_json);
    if ("resolution" in a) payload.resolution = a.resolution;
    if (r.kind === "issue") {
      if (!["open", "resolved"].includes(status) || status === "resolved" && !payload.resolution) bad("invalid issue status or resolution required");
    } else if ("status" in a || "resolution" in a) bad("status/resolution only valid for issues");
    run(tx, "UPDATE records SET title=?,body=?,status=?,payload_json=?,version=version+1,updated_at=? WHERE id=?", [a.title ?? r.title, a.body ?? r.body, status, canonicalJson(payload), t, r.id]);
    if ("links" in a) {
      run(tx, "DELETE FROM record_links WHERE record_id=?", [r.id]);
      links(tx, r.id, a.links);
    }
    if ("relations" in a) {
      run(tx, "DELETE FROM record_relations WHERE source_id=?", [r.id]);
      relations(tx, r.id, a.relations, {}, true);
    }
    if ("files" in a) {
      run(tx, "DELETE FROM file_refs WHERE record_id=?", [r.id]);
      for (const f of a.files) file2(tx, r.id, f);
    }
    data = { record_id: r.id, status };
  } else {
    const r = getRow(tx, "SELECT * FROM items WHERE id=? AND removed_at IS NULL", [a.item_id]);
    if (!r) throw new FactError("NOT_FOUND");
    if (a.expected_version !== r.version) throw new FactError("VERSION_CONFLICT");
    if (!(a.action === "start" ? ["planned", "blocked"] : ["planned", "active"]).includes(r.status)) throw new FactError("INVALID_TRANSITION");
    if (a.action === "start" && !canStart(tx, r)) throw new FactError("DEPENDENCY_BLOCKED");
    const status = a.action === "start" ? "active" : "blocked";
    run(tx, "UPDATE items SET status=?,version=version+1,updated_at=? WHERE id=?", [status, t, r.id]);
    data = { item_id: r.id, status };
  }
  return commitMutation(tx, { rawArgs: a, requestId: a.request_id, response: { ok: true, data }, operation: "record_" + a.action, targetType: "work", targetId: w.id, ...entityDiff(before, entityState(tx)) });
}

// src/views.ts
var encodeCursor = (v) => Buffer.from(canonicalJson(v)).toString("base64url");
function decodeCursor(v) {
  try {
    if (!/^[A-Za-z0-9_-]+$/.test(v)) throw 0;
    const x = JSON.parse(Buffer.from(v, "base64url").toString());
    if (!x || typeof x !== "object" || Array.isArray(x)) throw 0;
    return x;
  } catch {
    throw new FactError("VALIDATION_ERROR", "invalid cursor");
  }
}
function decodeHistoryCursor(token) {
  const c = decodeCursor(token);
  if (!Number.isSafeInteger(c.anchor) || !Number.isSafeInteger(c.last) || c.anchor < 0 || c.last < 0) throw new FactError("VALIDATION_ERROR", "invalid cursor");
  return c;
}
function decodeCollectionCursor(token, collection, revision) {
  const c = decodeCursor(token);
  if (c.revision !== revision) throw new FactError("CURSOR_STALE");
  if (c.collection !== collection || typeof c.last !== "string") throw new FactError("VALIDATION_ERROR", "invalid cursor");
  return c;
}
function handleRead(tx, a) {
  return a.action === "current" ? readCurrent(tx, a.limit) : a.action === "detail" ? readDetail(tx, a) : readHistory(tx, a);
}
function readOverview(config) {
  let works = [], freshness = "unavailable";
  try {
    const s = openStore(config.indexPath, { write: false, timeoutMs: config.sqliteTimeoutMs, gate: false });
    try {
      works = allRows(s, "SELECT * FROM work_registry ORDER BY slug");
    } finally {
      closeStore(s);
    }
    freshness = works.length ? "fresh" : "unverified";
    for (const r of works) {
      try {
        const db = openStore(r.db_path, { write: false, timeoutMs: config.sqliteTimeoutMs });
        try {
          const w = getRow(db, "SELECT id,revision FROM work");
          if (!w || w.id !== r.work_id || w.revision !== r.observed_revision) freshness = "stale";
        } finally {
          closeStore(db);
        }
      } catch {
        freshness = "stale";
      }
    }
  } catch {
  }
  return { ok: true, data: { works, catalog_freshness: freshness, generated_at: (/* @__PURE__ */ new Date()).toISOString() } };
}
function bounded(items, limit) {
  const rows = items.slice(0, limit);
  return { items: rows, page: { total: items.length, returned: rows.length, has_more: rows.length < items.length } };
}
function readCurrent(tx, limit = 20) {
  const work = getRow(tx, "SELECT * FROM work"), items = allRows(tx, "SELECT * FROM items WHERE removed_at IS NULL ORDER BY parent_id,position,id"), records = allRows(tx, "SELECT * FROM records ORDER BY created_at DESC,id DESC"), goals = items.filter((i) => i.kind === "goal"), tasks = items.filter((i) => i.kind === "task"), issues = records.filter((r) => r.kind === "issue" && r.status === "open"), decisions = records.filter((r) => r.kind === "decision" && r.status === "active");
  const counts = (xs) => ({ total: xs.length, done: xs.filter((i) => i.status === "done").length, cancelled: xs.filter((i) => i.status === "cancelled").length });
  const gc = counts(goals), tc = counts(tasks);
  return { ok: true, data: { work, counts: { goals: goals.length, tasks: tasks.length, done: gc.done + tc.done, cancelled: gc.cancelled + tc.cancelled, open_issues: issues.length }, goal_counts: gc, task_counts: tc, items: items.slice(0, limit), issues: issues.slice(0, limit), decisions: decisions.slice(0, limit), pending_proposals: allRows(tx, "SELECT id,kind,review_summary,created_at FROM proposals WHERE status='pending' ORDER BY created_at DESC,id DESC").slice(0, limit), suggested_startable: items.filter((i) => canStart(tx, i)).slice(0, limit), goals: bounded(goals, limit), tasks: bounded(tasks, limit), records: bounded(records, limit), outcomes: bounded(records.filter((r) => ["result", "experiment"].includes(r.kind)), limit), files: bounded(allRows(tx, "SELECT * FROM file_refs ORDER BY created_at DESC,id DESC"), limit), unassigned_legacy: bounded(records.filter((r) => r.legacy_payload && !getRow(tx, "SELECT 1 FROM record_links WHERE record_id=? AND item_id IS NOT NULL", [r.id])), limit), ...readLegacyInfo(tx), generated_at: (/* @__PURE__ */ new Date()).toISOString(), revision: work.revision } };
}
function readHistory(tx, a) {
  const limit = a.limit ?? 50, c = a.cursor ? decodeHistoryCursor(a.cursor) : { anchor: getRow(tx, "SELECT COALESCE(MAX(seq),0) AS n FROM history").n, last: 0 };
  if (!Number.isSafeInteger(c.anchor) || !Number.isSafeInteger(c.last) || c.anchor < 0 || c.last < 0) throw new FactError("VALIDATION_ERROR", "invalid cursor");
  const total = getRow(tx, "SELECT COUNT(*) AS n FROM history WHERE seq<=?", [c.anchor]).n, rows = allRows(tx, "SELECT * FROM history WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?", [c.last, c.anchor, limit + 1]), more = rows.length > limit, items = rows.slice(0, limit);
  return { ok: true, data: { items, page: { total, returned: items.length, has_more: more, cursor: more ? encodeCursor({ anchor: c.anchor, last: items.at(-1).seq }) : null } } };
}
function readDetail(tx, a) {
  if (a.collection) {
    const table = { items: "items", records: "records", files: "file_refs", proposals: "proposals" }[a.collection];
    if (!table) throw new FactError("VALIDATION_ERROR");
    const revision = getRow(tx, "SELECT revision FROM work").revision, limit = a.limit ?? 20;
    let last = "";
    if (a.cursor) {
      const c = decodeCollectionCursor(a.cursor, a.collection, revision);
      last = c.last;
    }
    const total = getRow(tx, `SELECT COUNT(*) AS n FROM ${table}`).n, rows = allRows(tx, `SELECT * FROM ${table} WHERE id>? ORDER BY id LIMIT ?`, [last, limit + 1]), more = rows.length > limit, items = rows.slice(0, limit);
    return { ok: true, data: { items, page: { total, returned: items.length, has_more: more, cursor: more ? encodeCursor({ revision, collection: a.collection, last: items.at(-1).id }) : null }, revision } };
  }
  const data = getRow(tx, "SELECT * FROM items WHERE id=?", [a.id]) ?? getRow(tx, "SELECT * FROM records WHERE id=?", [a.id]);
  if (!data) throw new FactError("NOT_FOUND");
  if ("payload_json" in data) {
    data.payload = JSON.parse(data.payload_json);
    data.links = allRows(tx, "SELECT item_id,relation FROM record_links WHERE record_id=? ORDER BY relation,item_id", [a.id]);
    data.relations = allRows(tx, "SELECT target_id,relation FROM record_relations WHERE source_id=? ORDER BY relation,target_id", [a.id]);
    data.files = allRows(tx, "SELECT * FROM file_refs WHERE record_id=? ORDER BY id", [a.id]).map((f) => ({ ...f, broken_reference: f.storage === "local" && !fs2.existsSync(path2.resolve(path2.dirname(tx.path), f.locator)) }));
  }
  return { ok: true, data };
}

// src/planning.ts
import { randomUUID as randomUUID3 } from "node:crypto";
var bad2 = (m) => {
  throw new FactError("VALIDATION_ERROR", m);
};
var allowed = { add: "ref kind parent_id parent_ref title description position", update: "item_id title description position parent_id", remove: "item_id", reorder: "item_id position", depend: "item_id item_ref requires_id requires_ref", undepend: "item_id requires_id", reopen: "item_id", cancel: "item_id", work_update: "name purpose kind", adopt: "acknowledge_migration_review" };
var ordered = (cs) => cs.map((c, i) => ({ c, i, n: c.op === "add" ? c.kind === "goal" ? 0 : 1 : 2 })).sort((a, b) => a.n - b.n || a.i - b.i).map((x) => x.c);
function validateDependencyGraph(tx, cs) {
  const refs = new Map(cs.filter((c) => c.op === "add" && c.ref).map((c) => ["@" + c.ref, c.kind]));
  const graph = /* @__PURE__ */ new Map();
  for (const r of allRows(tx, "SELECT * FROM dependencies")) {
    if (!graph.has(r.item_id)) graph.set(r.item_id, /* @__PURE__ */ new Set());
    graph.get(r.item_id).add(r.requires_id);
  }
  const id = (c, k) => c[k + "_id"] || (c[k + "_ref"] ? "@" + c[k + "_ref"] : null);
  const kind = (v) => {
    if (refs.has(v)) return refs.get(v);
    const r = getRow(tx, "SELECT * FROM items WHERE id=?", [v]);
    if (!r || r.removed_at !== null) bad2("missing/removed dependency item");
    return r.kind;
  };
  for (const c of cs) if (["depend", "undepend"].includes(c.op)) {
    const a = id(c, "item"), b = id(c, "requires");
    if (!a || !b || a === b) bad2("dependency self/missing");
    if (kind(a) !== kind(b)) bad2("mixed-kind dependency");
    if (c.op === "depend") {
      if (!graph.has(a)) graph.set(a, /* @__PURE__ */ new Set());
      graph.get(a).add(b);
    } else graph.get(a)?.delete(b);
  }
  const visiting = /* @__PURE__ */ new Set(), done = /* @__PURE__ */ new Set();
  const visit = (n) => {
    if (visiting.has(n)) bad2("dependency cycle");
    if (done.has(n)) return;
    visiting.add(n);
    for (const q of graph.get(n) ?? []) visit(q);
    visiting.delete(n);
    done.add(n);
  };
  for (const n of graph.keys()) visit(n);
}
function validateChanges(tx, cs) {
  if (!Array.isArray(cs) || !cs.length || cs.length > 100) bad2("invalid changes");
  const refs = /* @__PURE__ */ new Map();
  for (const c of ordered(cs)) {
    if (!allowed[c.op] || Object.keys(c).some((k) => k !== "op" && !allowed[c.op].split(" ").includes(k))) bad2("invalid change fields");
    if (c.position !== void 0 && (!Number.isSafeInteger(c.position) || c.position < 0)) bad2("invalid position");
    if (c.ref) {
      if (refs.has(c.ref)) bad2("duplicate ref");
      refs.set(c.ref, c);
    }
  }
  for (const c of cs) {
    if (c.op === "add") {
      if (!["goal", "task"].includes(c.kind) || !c.title) bad2("add requires kind/title");
      if (c.kind === "goal" && (c.parent_id || c.parent_ref)) bad2("goals cannot be nested");
      if (c.kind === "task" && !c.parent_id && !c.parent_ref && getRow(tx, "SELECT kind FROM work").kind === "project") bad2("project tasks require a goal");
      if (c.parent_ref && refs.get(c.parent_ref)?.kind !== "goal") bad2("invalid parent ref");
      if ("parent_id" in c) {
        const p = getRow(tx, "SELECT * FROM items WHERE id=?", [c.parent_id]);
        if (!p || p.kind !== "goal" || p.removed_at !== null) bad2("parent assignment requires live goal");
      }
    }
    if (c.op === "update" && "parent_id" in c) {
      const i = getRow(tx, "SELECT * FROM items WHERE id=?", [c.item_id]), p = getRow(tx, "SELECT * FROM items WHERE id=?", [c.parent_id]);
      if (!i || i.kind !== "task" || i.removed_at !== null || !p || p.kind !== "goal" || p.removed_at !== null) bad2("parent assignment requires live task and goal");
    }
  }
  validateDependencyGraph(tx, cs);
}
function capturePlanSnapshot(tx, cs) {
  const ids2 = /* @__PURE__ */ new Set(), parents = /* @__PURE__ */ new Set();
  let root = false;
  for (const c of cs) {
    for (const k of ["item_id", "requires_id", "parent_id"]) if (c[k]) ids2.add(c[k]);
    if (c.op === "add") {
      if (c.parent_id) parents.add(c.parent_id);
      else if (!c.parent_ref) root = true;
    } else if (["remove", "reorder"].includes(c.op) || c.op === "update" && "parent_id" in c) {
      const r = getRow(tx, "SELECT parent_id FROM items WHERE id=?", [c.item_id]);
      if (r?.parent_id) parents.add(r.parent_id);
      else if (r) root = true;
      if (c.op === "update") parents.add(c.parent_id);
    }
  }
  const items = {};
  for (const id of [.../* @__PURE__ */ new Set([...ids2, ...parents])].sort()) {
    const r = getRow(tx, "SELECT * FROM items WHERE id=?", [id]);
    if (!r) bad2("missing item: " + id);
    items[id] = [r.version, r.children_version, r.removed_at];
  }
  return { root: root ? getRow(tx, "SELECT root_items_version FROM work").root_items_version : null, items, parents: [...parents].sort() };
}
function assertPlanSnapshot(tx, base) {
  if (base.root !== null && getRow(tx, "SELECT root_items_version FROM work").root_items_version !== base.root) throw new FactError("STALE_PROPOSAL");
  for (const [id, v] of Object.entries(base.items)) {
    const r = getRow(tx, "SELECT * FROM items WHERE id=?", [id]);
    if (!r || canonicalJson([r.version, r.children_version, r.removed_at]) !== canonicalJson(v)) throw new FactError("STALE_PROPOSAL");
  }
}
function bump(tx, parent) {
  if (parent) run(tx, "UPDATE items SET children_version=children_version+1 WHERE id=?", [parent]);
  else run(tx, "UPDATE work SET root_items_version=root_items_version+1");
}
function apply(tx, cs) {
  const refs = {};
  for (const c of cs) if (c.op === "add" && c.ref) refs[c.ref] = randomUUID3();
  const t = (/* @__PURE__ */ new Date()).toISOString();
  for (const c of ordered(cs)) {
    const op = c.op;
    if (op === "add") {
      const parent = c.parent_id || refs[c.parent_ref] || null, pos = c.position ?? getRow(tx, "SELECT COALESCE(MAX(position),0)+1 AS n FROM items WHERE parent_id IS ? AND removed_at IS NULL", [parent]).n;
      run(tx, "INSERT INTO items(id,kind,parent_id,title,description,position,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", [refs[c.ref] || randomUUID3(), c.kind, parent, c.title, c.description ?? null, pos, t, t]);
      bump(tx, parent);
    } else if (op === "depend") run(tx, "INSERT INTO dependencies VALUES(?,?)", [c.item_id || refs[c.item_ref], c.requires_id || refs[c.requires_ref]]);
    else if (op === "undepend") run(tx, "DELETE FROM dependencies WHERE item_id=? AND requires_id=?", [c.item_id, c.requires_id]);
    else if (["update", "reorder", "remove", "reopen", "cancel"].includes(op)) {
      const r = getRow(tx, "SELECT * FROM items WHERE id=?", [c.item_id]);
      if (!r) throw new FactError("NOT_FOUND");
      if (op === "remove") {
        run(tx, "UPDATE items SET removed_at=?,version=version+1,updated_at=? WHERE id=?", [t, t, c.item_id]);
        bump(tx, r.parent_id);
      } else if (op === "cancel" || op === "reopen") run(tx, `UPDATE items SET status=?,${op === "reopen" ? "completed_at=NULL," : ""}version=version+1,updated_at=? WHERE id=?`, [op === "cancel" ? "cancelled" : "planned", t, c.item_id]);
      else {
        const fields2 = ["title", "description", "position", "parent_id"].filter((k) => k in c);
        if (!fields2.length) bad2("empty update");
        if ("parent_id" in c && c.parent_id !== r.parent_id) {
          bump(tx, r.parent_id);
          bump(tx, c.parent_id);
        }
        run(tx, `UPDATE items SET ${fields2.map((k) => k + "=?").join(",")},version=version+1,updated_at=? WHERE id=?`, [...fields2.map((k) => c[k]), t, c.item_id]);
      }
    } else if (op === "work_update") {
      const ks = ["name", "purpose", "kind"].filter((k) => k in c);
      if (!ks.length || c.kind && c.kind !== "project") bad2("invalid work update");
      run(tx, `UPDATE work SET ${ks.map((k) => k + "=?").join(",")},updated_at=?`, [...ks.map((k) => c[k]), t]);
    } else if (op === "adopt") {
      applyAdoption(tx, c);
    }
  }
  return refs;
}
function handlePlan(tx, a) {
  const prior = readReceipt(tx, a.request_id, a);
  if (prior) return prior;
  let p, cs;
  if (a.action === "propose") cs = a.changes;
  else {
    p = getRow(tx, "SELECT * FROM proposals WHERE id=?", [a.proposal_id]);
    if (!p) throw new FactError("NOT_FOUND");
    if (p.kind !== "plan") bad2("proposal is not a plan");
    if (["applied", "rejected"].includes(p.status)) {
      const saved = findTerminalReceipt(tx, p.id, { excludeRequestId: a.request_id });
      if (saved) return saved;
      throw new FactError("STALE_PROPOSAL");
    }
    if (p.status !== "pending") throw new FactError("STALE_PROPOSAL");
    cs = JSON.parse(p.payload_json);
  }
  const w = getRow(tx, "SELECT * FROM work");
  assertAdoption(tx, cs);
  if (w.status !== "imported_unreviewed") guardWork(tx);
  const before = entityState(tx);
  let response;
  if (a.action === "propose") {
    validateChanges(tx, cs);
    const base = capturePlanSnapshot(tx, cs), id = randomUUID3();
    run(tx, "INSERT INTO proposals VALUES(?,?,?,?,?,'pending',NULL,NULL,?,NULL)", [id, "plan", canonicalJson(cs), canonicalJson(base), a.review_summary ?? null, (/* @__PURE__ */ new Date()).toISOString()]);
    response = { ok: true, data: { proposal_id: id, changes: cs, needs_user_confirmation: true }, warnings: [], follow_up: ["Show the exact diff and ask the user to confirm."] };
    return commitMutation(tx, { rawArgs: a, requestId: a.request_id, response, operation: "plan_propose", targetType: "proposal", targetId: id, proposalId: id, after: cs });
  }
  if (a.action === "apply") {
    if (!a.confirmation?.summary) throw new FactError("CONFIRMATION_REQUIRED");
    assertPlanSnapshot(tx, JSON.parse(p.base_versions_json));
    validateChanges(tx, cs);
    const refs = apply(tx, cs);
    response = { ok: true, data: { proposal_id: p.id, status: "applied", refs } };
  } else response = { ok: true, data: { proposal_id: p.id, status: "rejected" } };
  run(tx, "UPDATE proposals SET status=?,confirmation_json=?,applied_at=? WHERE id=?", [a.action === "apply" ? "applied" : "rejected", canonicalJson(a.confirmation ?? null), (/* @__PURE__ */ new Date()).toISOString(), p.id]);
  return commitMutation(tx, { rawArgs: a, requestId: a.request_id, response, operation: "plan_" + a.action, targetType: "proposal", targetId: p.id, proposalId: p.id, ...entityDiff(before, entityState(tx)) });
}

// src/service.ts
import { createHash, randomUUID as randomUUID4 } from "node:crypto";
import * as fs3 from "node:fs";
import * as path3 from "node:path";
import { homedir } from "node:os";
function transaction(s, body, write) {
  if (s.transaction) throw new FactError("VALIDATION_ERROR", "nested transaction");
  let begun = false;
  try {
    s.db.exec(write ? "BEGIN IMMEDIATE" : "BEGIN");
    begun = true;
    s.transaction = true;
    const result = body(s);
    if (result && typeof result.then === "function") throw new FactError("VALIDATION_ERROR", "async transaction body");
    s.db.exec("COMMIT");
    begun = false;
    return result;
  } catch (e) {
    if (begun) try {
      s.db.exec("ROLLBACK");
    } catch (rollback) {
      console.error("rollback failed", rollback);
    }
    throw e;
  } finally {
    if (begun || s.transaction) {
      s.transaction = false;
    }
    closeStore(s);
  }
}
function withWrite(s, body) {
  return transaction(s, body, true);
}
function withRead(s, body) {
  return transaction(s, body, false);
}
function payloadHash(raw) {
  return createHash("sha256").update(canonicalJson(raw), "utf8").digest("hex");
}
function readReceipt(tx, requestId, raw) {
  const r = getRow(tx, "SELECT * FROM request_receipts WHERE request_id=?", [requestId]);
  if (!r) return null;
  if (r.payload_hash !== payloadHash(raw)) throw new FactError("REQUEST_CONFLICT");
  return JSON.parse(r.response_json);
}
function findTerminalReceipt(tx, proposalId, options = {}) {
  const r = getRow(tx, "SELECT response_json FROM request_receipts WHERE json_extract(response_json,'$.data.proposal_id')=? AND (? IS NULL OR request_id<>?) ORDER BY rowid DESC LIMIT 1", [proposalId, options.excludeRequestId ?? null, options.excludeRequestId ?? null]);
  return r ? JSON.parse(r.response_json) : null;
}
function fault(point) {
  if (process.env.FACT_M1_TEST_CHILD === "1" && process.env.FACT_FAULT_POINT === point) {
    if (point === "create_before_publish") process.exit(90);
    if (point === "create_after_publish") process.exit(91);
    throw new FactError(point === "history" ? "INJECTED_HISTORY_FAILURE" : "INJECTED_RECEIPT_FAILURE");
  }
}
function commitMutation(tx, spec) {
  const t = (/* @__PURE__ */ new Date()).toISOString();
  run(tx, "UPDATE work SET revision=revision+1,updated_at=?", [t]);
  const revision = getRow(tx, "SELECT revision FROM work").revision;
  const response = { ...spec.response, revision };
  run(tx, "INSERT INTO history(operation,target_type,target_id,before_json,after_json,request_id,proposal_id,source_session,occurred_at) VALUES(?,?,?,?,?,?,?,?,?)", [spec.operation, spec.targetType, spec.targetId, spec.before == null ? null : canonicalJson(spec.before), spec.after == null ? null : canonicalJson(spec.after), spec.requestId, spec.proposalId ?? null, null, t]);
  fault("history");
  run(tx, "INSERT INTO request_receipts VALUES(?,?,?,?)", [spec.requestId, payloadHash(spec.rawArgs), canonicalJson(response), revision]);
  fault("receipt");
  return response;
}
var entityTables = [["work", "id"], ["items", "id"], ["dependencies", "item_id,requires_id"], ["records", "id"], ["record_links", "record_id,item_id,relation"], ["record_relations", "source_id,target_id,relation"], ["file_refs", "id"], ["proposals", "id"]];
function entityState(tx) {
  return Object.fromEntries(entityTables.map(([table, order]) => [table, Object.fromEntries(allRows(tx, `SELECT * FROM ${table} ORDER BY ${order}`).map((r) => [String(r.id || Object.values(r).map((v) => v === null ? "None" : String(v)).join("|")), r]))]));
}
function entityDiff(before, after) {
  const old = {}, next = {};
  for (const t of Object.keys(before)) {
    for (const k of [.../* @__PURE__ */ new Set([...Object.keys(before[t]), ...Object.keys(after[t])])].sort()) {
      if (canonicalJson(before[t][k] ?? null) !== canonicalJson(after[t][k] ?? null)) {
        (old[t] ??= {})[k] = before[t][k] ?? null;
        (next[t] ??= {})[k] = after[t][k] ?? null;
      }
    }
  }
  return { before: old, after: next };
}
function guardWork(tx, options = {}) {
  const w = getRow(tx, "SELECT * FROM work");
  if (!w) throw new FactError("NOT_FOUND");
  if (w.status === "archived") throw new FactError("WORK_ARCHIVED");
  if (w.status === "completed" && !options.allowCompleted) throw new FactError("WORK_COMPLETED");
  if (w.status === "imported_unreviewed") throw new FactError("ADOPTION_REQUIRED");
  return w;
}
var catalogDDL = "CREATE TABLE IF NOT EXISTS work_registry(work_id TEXT PRIMARY KEY,slug TEXT,name TEXT,kind TEXT,root_path TEXT,db_path TEXT,purpose_excerpt TEXT,status TEXT,last_activity_at TEXT,observed_revision INTEGER,indexed_at TEXT); CREATE TABLE IF NOT EXISTS work_aliases(alias TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES work_registry(work_id));";
function namespace(index, id, slug, dbPath) {
  for (const r of allRows(index, "SELECT * FROM work_registry")) {
    if (r.work_id === id && path3.resolve(r.db_path) !== path3.resolve(dbPath)) throw new FactError("IDENTITY_CONFLICT");
    if (r.work_id !== id && ([r.work_id, r.slug].some((x) => [id, slug].includes(x)) || path3.resolve(r.db_path) === path3.resolve(dbPath))) throw new FactError("ALIAS_CONFLICT");
  }
  for (const r of allRows(index, "SELECT * FROM work_aliases")) if ([id, slug].includes(r.alias) && r.work_id !== id) throw new FactError("ALIAS_CONFLICT");
}
function upsert(index, dbPath, config) {
  const w = withRead(openStore(dbPath, { write: false, timeoutMs: config.sqliteTimeoutMs }), (s) => getRow(s, "SELECT * FROM work"));
  namespace(index, w.id, w.slug, dbPath);
  const prior = getRow(index, "SELECT slug FROM work_registry WHERE work_id=?", [w.id]);
  if (prior && prior.slug !== w.slug) run(index, "INSERT OR IGNORE INTO work_aliases VALUES(?,?)", [prior.slug, w.id]);
  run(index, "INSERT INTO work_registry VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(work_id) DO UPDATE SET slug=excluded.slug,name=excluded.name,kind=excluded.kind,root_path=excluded.root_path,db_path=excluded.db_path,purpose_excerpt=excluded.purpose_excerpt,status=excluded.status,last_activity_at=excluded.last_activity_at,observed_revision=excluded.observed_revision,indexed_at=excluded.indexed_at", [w.id, w.slug, w.name, w.kind, path3.dirname(dbPath), path3.resolve(dbPath), Array.from(w.purpose).slice(0, 200).join(""), w.status, w.updated_at, w.revision, (/* @__PURE__ */ new Date()).toISOString()]);
}
function rebuildCatalog(index, config) {
  const target = path3.join(path3.dirname(config.indexPath), "catalog.json"), tmp = target + "." + randomUUID4() + ".tmp";
  try {
    fs3.writeFileSync(tmp, JSON.stringify({ version: 1, works: allRows(index, "SELECT * FROM work_registry ORDER BY slug"), aliases: allRows(index, "SELECT * FROM work_aliases ORDER BY alias") }) + "\n");
    fs3.renameSync(tmp, target);
  } finally {
    fs3.rmSync(tmp, { force: true });
  }
}
function refreshCatalog(dbPath, config) {
  try {
    fs3.mkdirSync(path3.dirname(config.indexPath), { recursive: true });
    withWrite(openNewStore(config.indexPath, config.sqliteTimeoutMs), (s) => {
      s.db.exec(catalogDDL);
      upsert(s, dbPath, config);
    });
    withRead(openStore(config.indexPath, { write: false, timeoutMs: config.sqliteTimeoutMs, gate: false }), (s) => rebuildCatalog(s, config));
    return null;
  } catch (e) {
    console.error("fact-infra catalog refresh failed", e);
    return String(e);
  }
}
function resolveWork(work, config) {
  const expanded = work === "~" ? homedir() : work.startsWith("~/") ? path3.join(homedir(), work.slice(2)) : work;
  if (fs3.existsSync(expanded)) {
    const candidate = fs3.statSync(expanded).isDirectory() ? path3.join(expanded, "fact.db") : expanded;
    if (fs3.existsSync(candidate) && fs3.statSync(candidate).isFile()) return fs3.realpathSync(candidate);
  }
  if (fs3.existsSync(config.indexPath)) {
    const p = withRead(openStore(config.indexPath, { write: false, timeoutMs: config.sqliteTimeoutMs, gate: false }), (s) => getRow(s, "SELECT db_path FROM work_registry WHERE slug=? OR work_id=?", [work, work]) ?? getRow(s, "SELECT r.db_path FROM work_aliases a JOIN work_registry r ON a.work_id=r.work_id WHERE alias=?", [work]));
    if (p && fs3.existsSync(p.db_path) && fs3.statSync(p.db_path).isFile()) return fs3.realpathSync(p.db_path);
  }
  throw new FactError("VALIDATION_ERROR", "work cannot be resolved");
}
function fsync(p) {
  const fd = fs3.openSync(p, "r");
  try {
    fs3.fsyncSync(fd);
  } finally {
    fs3.closeSync(fd);
  }
}
function createWork(args, config, _ctx = {}) {
  const folder = path3.resolve(args.root_path ?? config.workRoot, args.kind === "project" ? "projects" : "work", args.slug), publicPath = path3.join(folder, "fact.db");
  let stage;
  let published = false;
  const replay = () => fs3.existsSync(publicPath) ? withRead(openStore(publicPath, { write: false, timeoutMs: config.sqliteTimeoutMs }), (s) => readReceipt(s, args.request_id, args)) : null;
  const prior = replay();
  if (prior) {
    refreshCatalog(publicPath, config);
    return prior;
  }
  if (fs3.existsSync(publicPath)) throw new FactError("VALIDATION_ERROR", "target exists without matching receipt");
  fs3.mkdirSync(path3.dirname(config.indexPath), { recursive: true });
  try {
    const result = withWrite(openNewStore(config.indexPath, config.sqliteTimeoutMs), (index) => {
      index.db.exec(catalogDDL);
      const lockedPrior = replay();
      if (lockedPrior) {
        upsert(index, publicPath, config);
        return lockedPrior;
      }
      if (fs3.existsSync(publicPath)) throw new FactError("VALIDATION_ERROR", "target exists");
      const id = randomUUID4(), t = (/* @__PURE__ */ new Date()).toISOString();
      namespace(index, id, args.slug, publicPath);
      fs3.mkdirSync(path3.dirname(folder), { recursive: true });
      stage = fs3.mkdtempSync(path3.join(path3.dirname(folder), ".fact-create-" + args.slug + "-"));
      const staged = path3.join(stage, "fact.db");
      withWrite(openNewStore(staged, config.sqliteTimeoutMs), (tx) => {
        initializeSchema(tx);
        run(tx, "INSERT INTO work VALUES(?,?,?,?,?,'active',0,0,?,?)", [id, args.slug, args.name || args.slug, args.purpose, args.kind, t, t]);
        commitMutation(tx, { rawArgs: args, requestId: args.request_id, response: { ok: true, data: { created: true }, work_id: id, db_path: publicPath, warnings: [{ code: "catalog_refresh_best_effort", message: "The catalog is derived and may be rebuilt independently from the published local database." }], follow_up: [] }, operation: "create", targetType: "work", targetId: id, after: args });
      });
      const checkpoint = openStore(staged, { write: true, timeoutMs: config.sqliteTimeoutMs });
      try {
        checkpoint.db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
      } finally {
        closeStore(checkpoint);
      }
      fsync(staged);
      fsync(stage);
      fault("create_before_publish");
      fs3.mkdirSync(folder, { recursive: true });
      try {
        fs3.linkSync(staged, publicPath);
        published = true;
      } catch (e) {
        const winner = replay();
        if (!winner) throw e;
        upsert(index, publicPath, config);
        fs3.rmSync(stage, { recursive: true, force: true });
        stage = void 0;
        return winner;
      }
      fsync(folder);
      fs3.rmSync(stage, { recursive: true });
      stage = void 0;
      fsync(path3.dirname(folder));
      fault("create_after_publish");
      upsert(index, publicPath, config);
      return replay();
    });
    refreshCatalog(publicPath, config);
    return result;
  } catch (e) {
    if (stage && !published && !(e instanceof FactError)) fs3.rmSync(stage, { recursive: true, force: true });
    throw e;
  } finally {
    if (stage && published) fs3.rmSync(stage, { recursive: true, force: true });
  }
}
var codes = new Set("VALIDATION_ERROR NOT_FOUND CONFLICT VERSION_CONFLICT STALE_PROPOSAL INVALID_TRANSITION DEPENDENCY_BLOCKED WORK_ARCHIVED WORK_COMPLETED ADOPTION_REQUIRED MIGRATION_REVIEW_ACK_REQUIRED MIGRATION_REQUIRED UNSUPPORTED_SCHEMA CURSOR_STALE IDENTITY_CONFLICT ALIAS_CONFLICT CONFIRMATION_REQUIRED DB_BUSY INJECTED_HISTORY_FAILURE INJECTED_RECEIPT_FAILURE".split(" "));
function mapError(error, tool2) {
  const message = error instanceof Error ? error.message : String(error);
  let code = error instanceof FactError ? error.tag : "VALIDATION_ERROR";
  if (code === "REQUEST_CONFLICT") code = "CONFLICT";
  if (!(error instanceof FactError) && tool2 !== "fact_create" && /locked|busy/i.test(message)) code = "DB_BUSY";
  if (!codes.has(code)) code = "VALIDATION_ERROR";
  return { ok: false, error: { code, message, details: error instanceof FactError ? error.details : {}, retryable: code === "DB_BUSY" } };
}
async function executeTool(name, raw, config, ctx = {}) {
  try {
    const args = validatePublic(name, raw);
    if (name === "fact_create") return createWork(args, config, ctx);
    if (name === "fact_read") {
      if (args.action === "overview") return readOverview(config);
      return withRead(openStore(resolveWork(args.work, config), { write: false, timeoutMs: config.sqliteTimeoutMs }), (tx) => handleRead(tx, args));
    }
    if (name === "fact_plan" || name === "fact_record" || name === "fact_finish") {
      const dbPath = resolveWork(args.work, config);
      let replay = false;
      const response = withWrite(openStore(dbPath, { write: true, timeoutMs: config.sqliteTimeoutMs }), (tx) => {
        const prior = readReceipt(tx, args.request_id, args);
        if (prior) {
          replay = true;
          return prior;
        }
        if (name === "fact_finish" && ["confirm", "continue"].includes(args.action) && args.proposal_id) {
          const p = getRow(tx, "SELECT status FROM proposals WHERE id=?", [args.proposal_id]);
          if (p && ["applied", "rejected"].includes(p.status)) {
            const terminal = findTerminalReceipt(tx, args.proposal_id);
            if (terminal) {
              replay = true;
              return terminal;
            }
          }
        }
        if (name === "fact_plan" && args.action !== "propose") {
          const p = getRow(tx, "SELECT status FROM proposals WHERE id=?", [args.proposal_id]);
          if (p && ["applied", "rejected"].includes(p.status)) replay = true;
        }
        return name === "fact_plan" ? handlePlan(tx, args) : name === "fact_finish" ? handleFinish(tx, args) : handleRecord(tx, args);
      });
      if (!replay && (name === "fact_record" || args.action !== "propose")) refreshCatalog(dbPath, config);
      return response;
    }
    throw new FactError("VALIDATION_ERROR", `NOT_IMPLEMENTED: ${name}/${args.action} is outside M2`);
  } catch (e) {
    if (ctx.toolCallId) console.error("fact-infra toolCallId=" + ctx.toolCallId);
    return mapError(e, name);
  }
}

// src/index.ts
var { defineToolPlugin } = createRequire(import.meta.url)("openclaw/plugin-sdk/tool-plugin");
var descriptions = { fact_create: "Create a work with a durable receipt", fact_read: "Read overview, current state, detail, or history", fact_plan: "Propose, apply, or reject a plan", fact_record: "Append or update records; start or block tasks", fact_finish: "Prepare, confirm, or continue completion" };
var index_default = defineToolPlugin({
  id: "fact-infra",
  name: "Fact Infrastructure",
  description: "Fact infrastructure v3",
  configSchema,
  tools: (tool2) => Object.entries(toolSchemas()).map(([name, parameters]) => tool2({ name, label: name, description: descriptions[name], parameters, execute: (params, config, ctx) => executeTool(name, params, parseConfig(config), ctx) }))
});
export {
  index_default as default
};
