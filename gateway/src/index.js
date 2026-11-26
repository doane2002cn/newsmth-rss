const ARTICLE_PATH = /^\/r\/([A-Za-z0-9_]{1,64})\/(\d{1,12})\/?$/;
const PAGE_VALUE = /^[1-9]\d{0,3}$/;
const NO_STORE = "no-store, no-cache, must-revalidate";

function json(body, status = 200, extraHeaders = {}, head = false) {
  const encoded = JSON.stringify(body);
  return new Response(head ? null : encoded, {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "content-length": String(new TextEncoder().encode(encoded).byteLength),
      "cache-control": NO_STORE,
      "x-content-type-options": "nosniff",
      ...extraHeaders,
    },
  });
}

function redirect(location, status = 302) {
  return new Response(null, {
    status,
    headers: {
      location,
      "cache-control": NO_STORE,
      expires: "0",
      pragma: "no-cache",
      "referrer-policy": "no-referrer",
      "x-robots-tag": "noindex, nofollow",
      "x-content-type-options": "nosniff",
    },
  });
}

function pageParameter(searchParams) {
  const pValues = searchParams.getAll("p");
  const pageValues = searchParams.getAll("page");
  if (pValues.length > 1 || pageValues.length > 1) {
    return { ok: false };
  }
  if (
    pValues.length === 1 &&
    pageValues.length === 1 &&
    pValues[0] !== pageValues[0]
  ) {
    return { ok: false };
  }

  const page = pValues[0] ?? pageValues[0] ?? null;
  if (page !== null && !PAGE_VALUE.test(page)) {
    return { ok: false };
  }
  return { ok: true, page };
}

export function articleTarget(pathname, searchParams) {
  const match = pathname.match(ARTICLE_PATH);
  if (!match) {
    return null;
  }

  const [, board, topic] = match;
  const parsedPage = pageParameter(searchParams);
  if (!parsedPage.ok) {
    throw new TypeError("invalid_page");
  }
  const target = new URL(
    `https://www.newsmth.net/nForum/article/${board}/${topic}`,
  );
  if (parsedPage.page !== null) {
    target.searchParams.set("p", parsedPage.page);
  }
  return target.toString();
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const head = request.method === "HEAD";

    if (request.method !== "GET" && request.method !== "HEAD") {
      return json({ error: "method_not_allowed" }, 405, {
        allow: "GET, HEAD",
      });
    }

    if (url.pathname === "/.well-known/assetlinks.json") {
      return json([], 200, {}, head);
    }

    if (url.pathname === "/healthz") {
      return json({ ok: true, service: "smth-go", version: 1 }, 200, {}, head);
    }

    let target;
    try {
      target = articleTarget(url.pathname, url.searchParams);
    } catch (error) {
      if (error instanceof TypeError && error.message === "invalid_page") {
        return json({ error: "invalid_page" }, 400, {}, head);
      }
      throw error;
    }
    if (!target) {
      return json({ error: "invalid_article_path" }, 400, {}, head);
    }

    return redirect(target);
  },
};
