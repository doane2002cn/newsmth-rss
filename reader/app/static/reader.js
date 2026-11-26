(() => {
  const root = document.documentElement;
  const body = document.body;
  const progress = document.getElementById("progress");
  const menu = document.getElementById("menu");
  const menuButton = document.getElementById("menuButton");
  const toast = document.getElementById("toast");
  const sessionButton = document.getElementById("sessionButton");
  const bookmarkButton = document.getElementById("bookmarkButton");
  const fontDown = document.getElementById("fontDown");
  const fontUp = document.getElementById("fontUp");
  let toastTimer = 0;

  function showToast(message) {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => toast.classList.remove("is-visible"), 1800);
  }

  function closeMenu() {
    if (!menu || !menuButton) return;
    menu.classList.remove("is-open");
    menuButton.setAttribute("aria-expanded", "false");
  }

  function setReaderSize(size) {
    const normalized = Math.max(16, Math.min(22, size));
    root.style.setProperty("--reader-size", `${normalized}px`);
    root.style.setProperty("--reader-size", `${normalized}px`);
    try {
      localStorage.setItem("smth-reader:font-size", String(normalized));
    } catch {
      // Local storage is optional.
    }
  }

  let savedSize = 18;
  try {
    savedSize = Number(localStorage.getItem("smth-reader:font-size")) || 18;
  } catch {
    savedSize = 18;
  }
  setReaderSize(savedSize);

  if (menuButton && menu) {
    menuButton.addEventListener("click", event => {
      event.stopPropagation();
      const open = menu.classList.toggle("is-open");
      menuButton.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("click", event => {
      if (!menu.contains(event.target)) closeMenu();
    });
  }

  if (sessionButton) {
    sessionButton.addEventListener("click", () => {
      const stale = body.dataset.stale === "true";
      showToast(
        stale
          ? "正在读取推送时保存的 RSS 内容快照，不会访问水木"
          : "正在读取阅读器保存的全文快照，不会访问水木"
      );
    });
  }

  if (fontDown) {
    fontDown.addEventListener("click", () => {
      const current = Number.parseFloat(getComputedStyle(root).getPropertyValue("--reader-size"));
      setReaderSize(current - 1);
    });
  }

  if (fontUp) {
    fontUp.addEventListener("click", () => {
      const current = Number.parseFloat(getComputedStyle(root).getPropertyValue("--reader-size"));
      setReaderSize(current + 1);
    });
  }

  if (bookmarkButton) {
    const bookmarkKey = `smth-reader:bookmark:${window.location.pathname}`;
    let bookmarked = false;
    try {
      bookmarked = localStorage.getItem(bookmarkKey) === "1";
    } catch {
      bookmarked = false;
    }
    bookmarkButton.classList.toggle("is-active", bookmarked);
    bookmarkButton.setAttribute("aria-pressed", String(bookmarked));
    bookmarkButton.addEventListener("click", () => {
      bookmarked = !bookmarked;
      bookmarkButton.classList.toggle("is-active", bookmarked);
      bookmarkButton.setAttribute("aria-pressed", String(bookmarked));
      try {
        if (bookmarked) localStorage.setItem(bookmarkKey, "1");
        else localStorage.removeItem(bookmarkKey);
      } catch {
        // Local storage is optional.
      }
      showToast(bookmarked ? "已收藏到本机" : "已取消收藏");
    });
  }

  document.querySelectorAll("[data-copy-link]").forEach(button => {
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(window.location.href);
        showToast("链接已复制");
      } catch {
        showToast("当前浏览器不允许自动复制，请长按地址栏复制");
      }
    });
  });

  const backButton = document.getElementById("backButton");
  if (backButton) {
    backButton.addEventListener("click", () => {
      if (history.length > 1) history.back();
      else window.location.href = document.referrer || "about:blank";
    });
  }

  function updateProgress() {
    if (!progress) return;
    const max = document.documentElement.scrollHeight - window.innerHeight;
    progress.style.width = `${max > 0 ? Math.min(100, window.scrollY / max * 100) : 0}%`;
  }

  window.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("resize", updateProgress, { passive: true });
  updateProgress();
})();
