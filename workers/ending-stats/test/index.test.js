// Run with: node --test "workers/ending-stats/test/*.test.js"
// D1 is stood in for by node:sqlite with the real schema, and fetch by the manifest.
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker from "../src/index.js";

const SITE = "https://mtmatt.csie.org";
const WORK = "/fiction/w/";
const [ONE, END_A, END_B] = [
  "/fiction/w/01/",
  "/fiction/w/02a/",
  "/fiction/w/02b/",
];
const MANIFEST = {
  works: { [WORK]: { chapters: [ONE, END_A, END_B], endings: [END_A, END_B] } },
};

globalThis.fetch = async () => Response.json(MANIFEST);

// The part of the D1 API the Worker uses.
function fakeD1(db) {
  return {
    prepare(sql) {
      const statement = db.prepare(sql);
      let values = [];
      return {
        bind(...args) {
          values = args;
          return this;
        },
        async run() {
          statement.run(...values);
        },
        async all() {
          return { results: statement.all(...values) };
        },
      };
    },
  };
}

let db;
let env;
let allowUploads;

beforeEach(() => {
  db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  allowUploads = true;
  env = {
    DB: fakeD1(db),
    UPLOADS: { limit: async () => ({ success: allowUploads }) },
    ALLOWED_ORIGINS: `${SITE}, http://localhost:1313`,
    MANIFEST_URL: `${SITE}/fiction/index.json`,
  };
});

function post(body, origin = SITE) {
  return worker.fetch(
    new Request("https://stats.example/endings", {
      method: "POST",
      headers: { Origin: origin, "Content-Type": "application/json" },
      body: typeof body === "string" ? body : JSON.stringify(body),
    }),
    env,
  );
}

function get(work = WORK, origin = SITE) {
  const url = `https://stats.example/endings?work=${encodeURIComponent(work)}`;
  return worker.fetch(new Request(url, { headers: { Origin: origin } }), env);
}

test("an empty work lists every ending at zero", async () => {
  const response = await get();
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), SITE);
  assert.deepEqual(await response.json(), {
    endings: [
      { ending: END_A, count: 0 },
      { ending: END_B, count: 0 },
    ],
    total: 0,
  });
});

test("an upload is counted and stored with its path", async () => {
  const response = await post({
    work: WORK,
    ending: END_A,
    path: [ONE, END_A],
  });
  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).endings[0], {
    ending: END_A,
    count: 1,
  });
  const [row] = db.prepare("SELECT work, ending, path FROM readings").all();
  assert.deepEqual(
    { ...row },
    { work: WORK, ending: END_A, path: JSON.stringify([ONE, END_A]) },
  );
});

test("an upload without a path stores none", async () => {
  await post({ work: WORK, ending: END_B });
  const [row] = db.prepare("SELECT path FROM readings").all();
  assert.equal(row.path, null);
  assert.equal((await (await get()).json()).total, 1);
});

test("the local preview origin is allowed", async () => {
  const response = await post(
    { work: WORK, ending: END_A },
    "http://localhost:1313",
  );
  assert.equal(response.status, 200);
});

test("readings outside the manifest are refused", async () => {
  const cases = [
    [{ work: "/fiction/other/", ending: END_A }, 404],
    [{ work: "__proto__", ending: END_A }, 404],
    [{ work: WORK, ending: ONE }, 400],
    [{ work: WORK, ending: END_A, path: [] }, 400],
    [{ work: WORK, ending: END_A, path: "01" }, 400],
    [{ work: WORK, ending: END_A, path: ["/fiction/x/", END_A] }, 400],
    [{ work: WORK, ending: END_A, path: [ONE, ONE, END_A] }, 400],
    [{ work: WORK, ending: END_A, path: [END_A, ONE] }, 400],
    ["not json", 400],
    ["null", 404],
  ];
  for (const [body, status] of cases) {
    assert.equal((await post(body)).status, status, JSON.stringify(body));
  }
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM readings").get().n, 0);
});

test("other sites can't upload or read", async () => {
  const upload = await post(
    { work: WORK, ending: END_A },
    "https://evil.example",
  );
  assert.equal(upload.status, 403);
  const read = await get(WORK, "https://evil.example");
  assert.equal(read.headers.get("Access-Control-Allow-Origin"), null);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM readings").get().n, 0);
});

test("uploads past the rate limit are refused", async () => {
  allowUploads = false;
  assert.equal((await post({ work: WORK, ending: END_A })).status, 429);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM readings").get().n, 0);
});

test("other paths and methods are refused", async () => {
  const other = await worker.fetch(new Request("https://stats.example/"), env);
  assert.equal(other.status, 404);
  const put = await worker.fetch(
    new Request("https://stats.example/endings", {
      method: "PUT",
      headers: { Origin: SITE },
    }),
    env,
  );
  assert.equal(put.status, 405);
  const preflight = await worker.fetch(
    new Request("https://stats.example/endings", {
      method: "OPTIONS",
      headers: { Origin: SITE },
    }),
    env,
  );
  assert.equal(preflight.status, 204);
  assert.equal(
    preflight.headers.get("Access-Control-Allow-Methods"),
    "GET, POST",
  );
});
