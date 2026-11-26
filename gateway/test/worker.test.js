import assert from "node:assert/strict";
import test from "node:test";

import worker, { articleTarget } from "../src/index.js";

function request(path = "/", method = "GET") {
  return new Request(`https://smth-go.example${path}`, { method });
}

test("redirects a valid article path and strips Feishu parameters", async () => {
  const response = await worker.fetch(
    request(
      "/r/Memory/193603?lk_jump_to_browser=true&" +
        "lk_mobile_jump_to_browser=true&p=2",
    ),
  );

  assert.equal(response.status, 302);
  assert.equal(
    response.headers.get("location"),
    "https://www.newsmth.net/nForum/article/Memory/193603?p=2",
  );
  assert.match(response.headers.get("cache-control"), /no-store/);
  assert.equal(response.headers.get("referrer-policy"), "no-referrer");
  assert.match(response.headers.get("x-robots-tag"), /noindex/);
});

test("accepts a trailing slash, page alias, and omitted page", () => {
  assert.equal(
    articleTarget("/r/NewExpress/47582810/", new URLSearchParams()),
    "https://www.newsmth.net/nForum/article/NewExpress/47582810",
  );
  assert.equal(
    articleTarget("/r/NewExpress/47582810", new URLSearchParams("page=3")),
    "https://www.newsmth.net/nForum/article/NewExpress/47582810?p=3",
  );
  assert.equal(
    articleTarget(
      "/r/NewExpress/47582810",
      new URLSearchParams("p=3&page=3"),
    ),
    "https://www.newsmth.net/nForum/article/NewExpress/47582810?p=3",
  );
});

test("rejects malformed board, topic, and page values", async () => {
  for (const path of [
    "/r/Memory/not-a-topic",
    "/r/Memory%2FBad/193603",
    "/r//193603",
    "/r/Memory/1234567890123",
    "/r/Memory/193603?p=0",
    "/r/Memory/193603?p=01",
    "/r/Memory/193603?page=10000",
    "/r/Memory/193603?p=not-a-page",
  ]) {
    const response = await worker.fetch(request(path));
    assert.equal(response.status, 400, path);
  }
});

test("rejects duplicate and conflicting page parameters", async () => {
  for (const query of [
    "p=2&p=2",
    "page=2&page=2",
    "p=2&page=3",
  ]) {
    const response = await worker.fetch(
      request(`/r/Memory/193603?${query}`),
    );
    assert.equal(response.status, 400, query);
  }
});

test("assetlinks is a direct uncached JSON 200 response for GET and HEAD", async () => {
  const response = await worker.fetch(request("/.well-known/assetlinks.json"));

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type"), /^application\/json/);
  assert.match(response.headers.get("cache-control"), /no-store/);
  assert.deepEqual(await response.json(), []);

  const head = await worker.fetch(
    request("/.well-known/assetlinks.json", "HEAD"),
  );
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
  assert.equal(
    head.headers.get("content-length"),
    response.headers.get("content-length"),
  );
});

test("health endpoint and method guard are deterministic", async () => {
  const health = await worker.fetch(request("/healthz", "HEAD"));
  assert.equal(health.status, 200);
  assert.equal(await health.text(), "");
  assert.ok(Number(health.headers.get("content-length")) > 0);

  const method = await worker.fetch(request("/r/Memory/193603", "POST"));
  assert.equal(method.status, 405);
  assert.equal(method.headers.get("allow"), "GET, HEAD");
});

test("HEAD on an article returns the same redirect without a body", async () => {
  const response = await worker.fetch(request("/r/Memory/193603?p=2", "HEAD"));
  assert.equal(response.status, 302);
  assert.equal(
    response.headers.get("location"),
    "https://www.newsmth.net/nForum/article/Memory/193603?p=2",
  );
  assert.equal(await response.text(), "");
});
