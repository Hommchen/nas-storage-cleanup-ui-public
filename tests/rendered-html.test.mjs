import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

async function renderedHtml() {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  return response.text();
}

test("renders the resource list with the final column model", async () => {
  const html = await renderedHtml();
  const pageSource = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(html, /<title>NAS 清理台 · PiNAS<\/title>/);
  assert.match(html, /<span>资源<\/span><span>媒体库<\/span><span>做种与保护<\/span>/);
  assert.match(html, /<span>实际占用<\/span><span>完整删除影响<\/span>/);
  assert.match(html, /仅看无做种限制/);
  assert.match(html, /名称待核/);
  assert.doesNotMatch(html, /<span>当前情况<\/span>/);
  assert.doesNotMatch(html, /<span>qB \/ PT<\/span>/);
  assert.doesNotMatch(html, /仅看会影响做种/);
  assert.doesNotMatch(html, /class="mini-cover/);
  assert.doesNotMatch(pageSource, /`，精确解除/);
});

test("does not embed a stale NAS snapshot in the frontend bundle", async () => {
  const html = await renderedHtml();

  assert.match(html, /正在读取资源清单/);
  assert.match(html, /尚未显示任何资源/);
  assert.doesNotMatch(html, /权力的游戏|Game of Thrones/);
});

test("ships a sanitized sample snapshot without private NAS data", async () => {
  const snapshotUrl = new URL(
    "./fixtures/resource-snapshot.sample.json",
    import.meta.url,
  );
  const raw = await readFile(snapshotUrl, "utf8");
  const snapshot = JSON.parse(raw);
  const got = snapshot.resources.find(
    (item) => item.englishTitle === "Example Series",
  );

  assert.equal(snapshot.schemaVersion, 2);
  assert.match(snapshot.snapshotId, /^snap_[a-f0-9]{24}$/);
  assert.equal(snapshot.source, "PiNAS GitHub sample snapshot");
  assert.equal(snapshot.resources.length, 3);
  assert.ok(snapshot.stats.qbTasks > 0);
  assert.equal(snapshot.stats.hrSourceAvailable, true);
  assert.ok(snapshot.stats.hrActiveTitles > 0);
  assert.ok(snapshot.stats.hrMatchedQbTasks > 0);
  assert.ok(
    snapshot.stats.hrMatchedQbTasks <= snapshot.stats.hrActiveTitles,
  );
  assert.equal(got.title, "示例剧集");
  assert.equal(got.librarySummary, "已入库 · 8 季");
  assert.match(got.id, /^res_[a-f0-9]{20}$/);
  assert.equal(
    new Set(snapshot.resources.map((item) => item.id)).size,
    snapshot.resources.length,
  );
  assert.equal(got.seedTasks.length, 3);
  assert.deepEqual(
    got.seedTasks.map((task) => `${task.status}:${task.site}:${task.scope}`),
    [
      "自发布:示例站 A:S01",
      "H&R 保护:示例站 B:S01",
      "做种中:示例站 C:S01–S08 全季合集",
    ],
  );
  assert.doesNotMatch(raw, /权力的游戏|Game of Thrones/);
  assert.doesNotMatch(raw, /\/mnt\//);
  assert.doesNotMatch(raw, /passkey|tracker|content_path|infohash/i);
  assert.doesNotMatch(raw, /"_private"/);
  assert.doesNotMatch(raw, /\b[a-f0-9]{40}\b/i);
});

test("reports exact H&R gaps and locks every recovery candidate", async () => {
  const snapshotUrl = new URL(
    "./fixtures/resource-snapshot.sample.json",
    import.meta.url,
  );
  const snapshot = JSON.parse(await readFile(snapshotUrl, "utf8"));
  const pending = snapshot.resources.filter((item) => item.hrPending);

  assert.equal(snapshot.stats.hrSourceAvailable, true);
  assert.ok(snapshot.stats.hrMissingUncovered >= 0);
  assert.ok(snapshot.stats.hrMissingUnassigned >= 0);
  assert.ok(
    snapshot.stats.hrMissingUncovered <= snapshot.stats.hrMissingQbTasks,
  );
  assert.ok(
    snapshot.stats.hrMissingUnassigned <= snapshot.stats.hrMissingQbTasks,
  );
  assert.equal(
    snapshot.stats.hrActiveTitles,
    snapshot.stats.hrMatchedQbTasks + snapshot.stats.hrMissingQbTasks,
  );
  assert.ok(
    pending.length >=
      Math.max(
        snapshot.stats.hrRecoveryCandidates,
        snapshot.stats.hrMissingLinkedResources,
      ),
  );
  assert.ok(pending.every((item) => item.protected));
  assert.ok(
    pending.every((item) =>
      item.seedTasks?.some((task) =>
        ["待核 H&R", "H&R 缺失"].includes(task.status),
      ),
    ),
  );
});

test("does not turn unassigned H&R gaps into a global UI lock", async () => {
  const pageSource = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(
    pageSource,
    /hasUnassignedHr\s*&&\s*item\.qbSummary\s*===\s*"无 qB 任务"/,
  );
  assert.doesNotMatch(pageSource, /完整删除已锁定/);
  assert.match(pageSource, /不影响无关资源独立审阅/);
  assert.match(pageSource, /未定位 H&R 只作风险提示，不全局锁定/);
});

test("locks every name or identity conflict and merges duplicate series rows", async () => {
  const snapshotUrl = new URL(
    "./fixtures/resource-snapshot.sample.json",
    import.meta.url,
  );
  const snapshot = JSON.parse(await readFile(snapshotUrl, "utf8"));
  const unverified = snapshot.resources.filter(
    (item) => item.metadataVerified !== true,
  );
  const unsettled = snapshot.resources.filter(
    (item) => item.englishTitle === "Unsettled Example",
  );

  assert.equal(
    unverified.length,
    snapshot.stats.metadataUnverifiedResources,
  );
  assert.ok(unverified.length > 0);
  assert.ok(unverified.every((item) => item.protected));
  assert.equal(unsettled.length, 1);
  assert.equal(unsettled[0].metadataVerified, false);
  assert.equal(unsettled[0].impactTitle, "名称待核，暂不可清理");
});
