(() => {
  const root = document.body;
  const frame = document.getElementById("vncFrame");
  const status = document.getElementById("adminStatus");
  const statusText = document.getElementById("adminStatusText");
  const alert = document.getElementById("adminAlert");
  const openLogin = document.getElementById("openLogin");
  const reloadFrame = document.getElementById("reloadFrame");
  const logout = document.getElementById("logoutButton");
  const nextUrl = root?.dataset.nextUrl || "";
  const challengeMode = root?.dataset.challengeMode === "1";
  const target = {
    board: root?.dataset.targetBoard || "",
    topic: root?.dataset.targetTopic || "",
    page: root?.dataset.targetPage || "1",
  };
  let redirected = false;
  let pageCheckInFlight = false;

  function setStatus(text, color) {
    if (statusText) statusText.textContent = text;
    if (status) status.style.setProperty("--status-color", color);
  }

  function setAlert(text) {
    if (alert) alert.textContent = text;
  }

  function reloadVnc(delay = 0) {
    window.setTimeout(() => {
      if (frame) frame.src = frame.src;
    }, delay);
  }

  function targetParams() {
    const params = new URLSearchParams({
      page: target.page || "1",
      challenge: challengeMode ? "1" : "0",
    });
    if (target.board) params.set("board", target.board);
    if (target.topic) params.set("topic", target.topic);
    if (nextUrl) params.set("next", nextUrl);
    return params;
  }

  function openRequestedPage() {
    return fetch(`/admin/open-login?${targetParams().toString()}`, {
      method: "POST",
      headers: { "Accept": "application/json" },
      credentials: "same-origin",
    });
  }

  if (openLogin) {
    openLogin.addEventListener("click", async () => {
      openLogin.disabled = true;
      setStatus(challengeMode ? "正在打开验证页" : "正在打开登录页", "#9a5b19");
      try {
        const response = await openRequestedPage();
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (data.redirect) {
          window.location.assign(data.redirect);
          return;
        }
        setStatus(challengeMode ? "等待输入验证码" : "登录页已打开", "#248b6c");
        setAlert(
          challengeMode
            ? "已重新打开当前文章。请在下方输入验证码，验证成功会自动返回正文。"
            : "登录页已打开。请在下方完成账号登录和滑块验证。"
        );
        reloadVnc(500);
        window.setTimeout(checkPageStatus, 1200);
      } catch {
        setStatus(challengeMode ? "打开验证页失败" : "打开登录页失败", "#a3472d");
      } finally {
        openLogin.disabled = false;
      }
    });
  }

  if (reloadFrame) {
    reloadFrame.addEventListener("click", () => {
      setStatus("正在重连", "#9a5b19");
      reloadVnc();
      window.setTimeout(() => {
        checkPageStatus();
        checkSession();
      }, 1800);
    });
  }

  if (logout) {
    logout.addEventListener("click", async () => {
      await fetch("/logout", { method: "POST", credentials: "same-origin" });
      window.location.href = "/";
    });
  }

  async function checkSession() {
    if (challengeMode) return;
    try {
      const response = await fetch("/admin/session-status", {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.rate_limited) {
        setStatus("水木暂时限流", "#a3472d");
      } else if (data.valid) {
        setStatus(data.user ? `已登录 ${data.user}` : "会话有效", "#248b6c");
      } else {
        setStatus("尚未登录", "#a3472d");
      }
    } catch {
      setStatus("状态检查失败", "#a3472d");
    }
  }

  async function checkPageStatus() {
    if (pageCheckInFlight) return;
    pageCheckInFlight = true;
    try {
      const response = await fetch("/admin/page-status", {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.rate_limited) {
        setStatus("水木暂时限流", "#a3472d");
        setAlert("远程画面显示水木正在限流，这不是登录失效。请稍后再试。");
      } else if (data.challenge) {
        setStatus("等待输入验证码", "#9a5b19");
      } else if (data.article_ready) {
        setStatus("验证完成，正在返回文章", "#248b6c");
        setAlert("已验证当前文章，正在返回阅读页。");
        if (nextUrl && !redirected) {
          redirected = true;
          window.setTimeout(() => window.location.assign(nextUrl), 700);
        }
      } else if (data.logged_in) {
        setStatus("已登录，可返回文章", "#248b6c");
      } else {
        setStatus("请在远程画面完成验证", "#9a5b19");
      }
    } catch {
      setStatus("画面状态检查失败", "#a3472d");
    } finally {
      pageCheckInFlight = false;
    }
  }

  window.setTimeout(checkPageStatus, 800);
  window.setTimeout(checkSession, 1200);
  window.setInterval(checkPageStatus, 2000);
})();
