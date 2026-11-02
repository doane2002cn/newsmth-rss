(() => {
  const frame = document.getElementById("vncFrame");
  const status = document.getElementById("adminStatus");
  const statusText = document.getElementById("adminStatusText");
  const openLogin = document.getElementById("openLogin");
  const reloadFrame = document.getElementById("reloadFrame");
  const logout = document.getElementById("logoutButton");

  function setStatus(text, color) {
    if (statusText) statusText.textContent = text;
    if (status) status.style.setProperty("--status-color", color);
  }

  function reloadVnc(delay = 0) {
    window.setTimeout(() => {
      if (frame) frame.src = frame.src;
    }, delay);
  }

  if (openLogin) {
    openLogin.addEventListener("click", async () => {
      openLogin.disabled = true;
      setStatus("正在打开登录页", "#9a5b19");
      try {
        const response = await fetch("/admin/open-login", {
          method: "POST",
          headers: { "Accept": "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        setStatus("登录页已打开", "#248b6c");
        reloadVnc(500);
      } catch {
        setStatus("打开登录页失败", "#a3472d");
      } finally {
        openLogin.disabled = false;
      }
    });
  }

  if (reloadFrame) {
    reloadFrame.addEventListener("click", () => {
      setStatus("正在重连", "#9a5b19");
      reloadVnc();
      window.setTimeout(checkSession, 1800);
    });
  }

  if (logout) {
    logout.addEventListener("click", async () => {
      await fetch("/logout", { method: "POST", credentials: "same-origin" });
      window.location.href = "/";
    });
  }

  async function checkSession() {
    try {
      const response = await fetch("/admin/session-status", {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data.valid) {
        setStatus(data.user ? `已登录 ${data.user}` : "会话有效", "#248b6c");
      } else {
        setStatus("尚未登录", "#a3472d");
      }
    } catch {
      setStatus("状态检查失败", "#a3472d");
    }
  }

  window.setTimeout(checkSession, 800);
  window.setInterval(checkSession, 30000);
})();
