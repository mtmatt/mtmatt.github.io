// Ending statistics for branching fiction. Readers who choose to add their
// ending send it, and maybe the chapters they passed, and anyone can read the
// totals. Only works, endings and chapters in the site's manifest are accepted.
// Nothing about the reader is stored: no IP, no cookie, only the day.
//
// GET  /endings?work=/fiction/<work>/  -> the work's distribution
// POST /endings {work, ending, path?}  -> records one reading, then the same

const MANIFEST_TTL_MS = 5 * 60 * 1000;

let cachedManifest = null;
let cachedAt = 0;

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin");
    const allowed = allowedOrigins(env).includes(origin);
    const cors = allowed
      ? {
          "Access-Control-Allow-Origin": origin,
          "Access-Control-Allow-Methods": "GET, POST",
          "Access-Control-Allow-Headers": "Content-Type",
          Vary: "Origin",
        }
      : { Vary: "Origin" };
    const reply = (status, body) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json", ...cors },
      });

    try {
      return await handle(request, env, allowed, cors, reply);
    } catch (error) {
      console.error(error);
      return reply(503, { error: "statistics unavailable" });
    }
  },
};

async function handle(request, env, allowed, cors, reply) {
  const url = new URL(request.url);
  if (url.pathname !== "/endings") return reply(404, { error: "not found" });
  if (request.method === "OPTIONS")
    return new Response(null, { status: 204, headers: cors });

  if (request.method === "GET") {
    const work = await findWork(env, url.searchParams.get("work"));
    if (!work) return reply(404, { error: "unknown work" });
    return reply(200, await distribution(env, work));
  }

  if (request.method === "POST") {
    // Browsers always send Origin on a cross-site POST, so this keeps out other
    // sites' pages. A script can fake it, which the rate limit is for.
    if (!allowed) return reply(403, { error: "origin not allowed" });
    const ip = request.headers.get("CF-Connecting-IP") ?? "";
    const { success } = await env.UPLOADS.limit({ key: ip });
    if (!success) return reply(429, { error: "too many uploads" });

    let body;
    try {
      body = await request.json();
    } catch {
      return reply(400, { error: "body is not JSON" });
    }
    const work = await findWork(env, body?.work);
    if (!work) return reply(404, { error: "unknown work" });
    const problem = checkReading(work, body);
    if (problem) return reply(400, { error: problem });

    const path = Array.isArray(body.path) ? JSON.stringify(body.path) : null;
    const day = new Date().toISOString().slice(0, 10);
    await env.DB.prepare(
      "INSERT INTO readings (work, ending, path, day) VALUES (?, ?, ?, ?)",
    )
      .bind(work.id, body.ending, path, day)
      .run();
    return reply(200, await distribution(env, work));
  }

  return reply(405, { error: "method not allowed" });
}

function allowedOrigins(env) {
  return env.ALLOWED_ORIGINS.split(",").map((origin) => origin.trim());
}

// The work as the manifest lists it, or null for anything else.
async function findWork(env, id) {
  if (typeof id !== "string") return null;
  const manifest = await loadManifest(env);
  // hasOwn, so names such as __proto__ aren't found on the object's prototype.
  return Object.hasOwn(manifest.works, id)
    ? { id, ...manifest.works[id] }
    : null;
}

async function loadManifest(env) {
  if (cachedManifest && Date.now() - cachedAt < MANIFEST_TTL_MS)
    return cachedManifest;
  const response = await fetch(env.MANIFEST_URL);
  if (!response.ok) throw new Error(`manifest: HTTP ${response.status}`);
  cachedManifest = await response.json();
  cachedAt = Date.now();
  return cachedManifest;
}

// Why a reading can't be recorded, or null if it can. A path lists chapters in
// the order read, each at most once, and finishes at the ending.
function checkReading(work, { ending, path }) {
  if (!work.endings.includes(ending)) return "unknown ending";
  if (path === undefined || path === null) return null;
  if (!Array.isArray(path) || path.length === 0)
    return "path must be a list of chapters";
  if (!path.every((chapter) => work.chapters.includes(chapter)))
    return "unknown chapter in path";
  if (new Set(path).size !== path.length) return "a chapter repeats in path";
  if (path.at(-1) !== ending) return "path must finish at the ending";
  return null;
}

// Every ending of the work in the manifest's order, with how many readers added it.
async function distribution(env, work) {
  const { results } = await env.DB.prepare(
    "SELECT ending, COUNT(*) AS count FROM readings WHERE work = ? GROUP BY ending",
  )
    .bind(work.id)
    .all();
  const counts = new Map(results.map((row) => [row.ending, row.count]));
  const endings = work.endings.map((ending) => ({
    ending,
    count: counts.get(ending) ?? 0,
  }));
  const total = endings.reduce((sum, { count }) => sum + count, 0);
  return { endings, total };
}
