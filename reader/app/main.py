from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.websockets import WebSocketDisconnect

try:
    from websockets.asyncio.client import connect as websocket_connect
except ImportError:  # pragma: no cover - compatibility with older websockets
    from websockets import connect as websocket_connect

from .browser import BrowserManager, SMTHRateLimited, SMTHUnavailable
from .cache import BinaryFileCache, FileCache
from .config import Settings
from .models import ArticlePage
from .parser import (
    GuestVerificationRequired,
    ArticleParseError,
    SMTHLoginRequired,
    format_cache_age,
    is_allowed_image_url,
    normalize_line_breaks,
    parse_article,
)
from .signing import (
    InvalidToken,
    admin_session_token,
    article_token,
    image_token,
    loads_typed,
)

LOGGER = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
SESSION_COOKIE = "smth_reader_admin"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
VNC_REQUEST_HEADERS = HOP_BY_HOP_HEADERS | {
    "accept-encoding",
    "cookie",
    "host",
}
VNC_RESPONSE_HEADERS = HOP_BY_HOP_HEADERS | {
    "set-cookie",
}


def _safe_next(value: str | None) -> str:
    if not value or not value.startswith("/a/") or value.startswith("//"):
        return ""
    return value


def _article_cache_key(board: str, topic: str, page: int) -> str:
    raw = f"{board}\0{topic}\0{page}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _format_session_ttl(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return f"{days} 天"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"{hours} 小时"
    return f"{max(1, seconds // 60)} 分钟"


def _plain_text_length(html: str) -> int:
    text = BeautifulSoup(html, "html.parser").get_text("", strip=True)
    return len(re.sub(r"\s+", "", text))


def _session_token(settings: Settings) -> str:
    return admin_session_token(
        settings.secret,
        int(time.time()) + settings.admin_session_ttl_seconds,
    )


def _valid_admin_session(request: Request, settings: Settings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return False
    try:
        loads_typed(token, settings.secret, "admin-session")
    except InvalidToken:
        return False
    return True


def _valid_admin_websocket(websocket: WebSocket, settings: Settings) -> bool:
    token = websocket.cookies.get(SESSION_COOKIE, "")
    if not token:
        return False
    try:
        loads_typed(token, settings.secret, "admin-session")
    except InvalidToken:
        return False
    return True


def _consume_login_nonce(settings: Settings, nonce: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,96}", nonce):
        return False
    directory = settings.login_nonce_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / nonce
    try:
        descriptor = path.open("x", encoding="ascii")
    except FileExistsError:
        return False
    try:
        descriptor.write(str(int(time.time())))
    finally:
        descriptor.close()
    return True


def _set_session_cookie(
    response: Response,
    settings: Settings,
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        _session_token(settings),
        max_age=settings.admin_session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _security_headers(response: Response) -> None:
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; "
        "frame-src 'self'; "
        "font-src 'self' data:; "
        "base-uri 'none'; form-action 'self'"
    )
    response.headers["Cache-Control"] = "private, no-store"


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings.from_env()
    article_cache = FileCache(app_settings.cache_dir)
    image_cache = BinaryFileCache(app_settings.image_cache_dir)
    app_settings.login_nonce_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        manager: BrowserManager | None = None
        application.state.browser_error = ""
        if app_settings.browser_enabled:
            manager = BrowserManager(app_settings)
            try:
                await manager.start()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("browser startup failed")
                application.state.browser_error = f"{type(exc).__name__}: {exc}"
                await manager.stop()
                manager = None
        application.state.browser = manager
        try:
            yield
        finally:
            if manager:
                await manager.stop()

    application = FastAPI(
        title="SMTH Reader",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.settings = app_settings
    application.state.article_cache = article_cache
    application.state.image_cache = image_cache
    application.state.browser = None
    application.state.browser_error = ""
    application.mount(
        "/static",
        StaticFiles(directory=str(APP_DIR / "static")),
        name="static",
    )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        _security_headers(response)
        return response

    @application.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        manager = request.app.state.browser
        return JSONResponse(
            {
                "ok": bool(manager),
                "browser_ready": bool(manager),
                "browser_mode": manager.mode if manager else "",
                "browser_error": request.app.state.browser_error,
                "article_cache_entries": article_cache.count(),
                "image_cache_entries": image_cache.count(),
                "time": int(time.time()),
            }
        )

    @application.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(
            status_code=204,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def render_message(
        request: Request,
        *,
        status_code: int,
        eyebrow: str,
        title: str,
        description: str,
        action_url: str = "",
        action_label: str = "",
        secondary_url: str = "",
        secondary_label: str = "",
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="message.html",
            context={
                "status_code": status_code,
                "eyebrow": eyebrow,
                "title": title,
                "description": description,
                "action_url": action_url,
                "action_label": action_label,
                "secondary_url": secondary_url,
                "secondary_label": secondary_label,
            },
            status_code=status_code,
        )

    async def load_article(
        request: Request,
        board: str,
        topic: str,
        page: int,
    ) -> tuple[ArticlePage, int, bool, str, str]:
        key = _article_cache_key(board, topic, page)
        cached = article_cache.read(key)
        cached_page: ArticlePage | None = None
        cached_age = 0
        if cached:
            try:
                cached_page = ArticlePage.from_dict(cached[0])
                for post in cached_page.posts:
                    post.body_html = normalize_line_breaks(post.body_html)
                cached_age = cached[1]
            except (KeyError, TypeError, ValueError):
                cached_page = None
        if cached_page and cached_age <= app_settings.article_ttl_seconds:
            return cached_page, cached_age, False, "", "HIT"

        manager: BrowserManager | None = request.app.state.browser
        if not manager:
            error = "浏览器会话尚未就绪"
            if cached_page and cached_age <= app_settings.stale_ttl_seconds:
                return cached_page, cached_age, True, error, "STALE"
            raise SMTHUnavailable(error)

        try:
            html = await manager.fetch_article_html(board, topic, page)
            parsed = parse_article(
                html,
                board=board,
                topic=topic,
                page=page,
                fallback_title="",
                base_url=f"{app_settings.smth_base}/nForum/article/{board}/{topic}",
                image_url_factory=lambda source: "/i/"
                + image_token(
                    app_settings.secret,
                    source,
                    int(time.time()) + app_settings.image_token_ttl_seconds,
                ),
                fetched_at=int(time.time()),
            )
            article_cache.write(key, parsed.to_dict())
            return parsed, 0, False, "", "MISS"
        except SMTHRateLimited:
            raise
        except SMTHLoginRequired:
            error = "水木登录会话已失效"
            if cached_page and cached_age <= app_settings.stale_ttl_seconds:
                return cached_page, cached_age, True, error, "STALE"
            raise
        except GuestVerificationRequired:
            error = "水木要求完成一次阅读验证"
            if cached_page and cached_age <= app_settings.stale_ttl_seconds:
                return cached_page, cached_age, True, error, "STALE"
            raise
        except (SMTHUnavailable, ArticleParseError):
            error = "水木暂时无法提供这篇文章"
            if cached_page and cached_age <= app_settings.stale_ttl_seconds:
                return cached_page, cached_age, True, error, "STALE"
            raise

    @application.get("/a/{token}", response_class=HTMLResponse)
    async def article_view(
        request: Request,
        token: str,
        p: int = 1,
    ) -> HTMLResponse:
        try:
            payload = loads_typed(token, app_settings.secret, "article")
        except InvalidToken:
            return render_message(
                request,
                status_code=410,
                eyebrow="LINK EXPIRED",
                title="这个阅读链接已失效",
                description=(
                    "链接带有有效期。请在飞书里重新获取最新卡片，"
                    "新的入口会自动使用当前阅读会话。"
                ),
            )

        board = str(payload.get("board") or "")
        topic = str(payload.get("topic") or "")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", board) or not re.fullmatch(
            r"\d{1,32}", topic
        ):
            return render_message(
                request,
                status_code=400,
                eyebrow="INVALID LINK",
                title="链接内容不完整",
                description="请回到飞书重新打开水木推荐卡片。",
            )

        page_number = max(1, min(p, 999))
        try:
            article, cache_age, stale, notice, cache_status = await load_article(
                request, board, topic, page_number
            )
        except SMTHLoginRequired:
            return render_message(
                request,
                status_code=401,
                eyebrow="LOGIN REQUIRED",
                title="水木登录会话已失效。",
                description=(
                    "阅读网关已保存的登录 Cookie 不再有效。"
                    "点击下方按钮重新登录，登录成功后会继续使用同一会话。"
                ),
                action_url=f"/relogin/{token}?p={page_number}",
                action_label="重新登录水木",
            )
        except GuestVerificationRequired:
            return render_message(
                request,
                status_code=401,
                eyebrow="READ CHALLENGE",
                title="水木需要再完成一次阅读验证。",
                description=(
                    "水木会对部分文章单独弹出验证码。点击下方按钮后，"
                    "远程浏览器会直接打开当前文章，完成验证即自动返回正文。"
                ),
                action_url=f"/relogin/{token}?{urlencode({'challenge': 1, 'p': page_number})}",
                action_label="完成阅读验证",
            )
        except SMTHRateLimited:
            LOGGER.warning("article request is rate limited by SMTH")
            return render_message(
                request,
                status_code=502,
                eyebrow="UPSTREAM RATE LIMIT",
                title="水木暂时限制了访问频率。",
                description=(
                    "这不是登录失效，通常与短时间内连续预热或打开文章过多有关。"
                    "请稍后重试，现有缓存和登录会话不受影响。"
                ),
                action_url=f"/a/{token}?p={page_number}",
                action_label="稍后重试",
            )
        except (SMTHUnavailable, ArticleParseError) as exc:
            LOGGER.warning("article unavailable: %s", type(exc).__name__)
            return render_message(
                request,
                status_code=502,
                eyebrow="FETCH INTERRUPTED",
                title="这次没有取到正文",
                description=(
                    "阅读网关触发了站点保护或上游暂时不可用。"
                    "稍后重试不会影响已经缓存的文章。"
                ),
                action_url=f"/a/{token}?p={page_number}",
                action_label="重新尝试",
            )

        if not article.posts:
            return render_message(
                request,
                status_code=502,
                eyebrow="EMPTY ARTICLE",
                title="文章页没有可显示的正文",
                description="上游返回的页面结构可能发生了变化，请稍后重试。",
                action_url=f"/a/{token}?p={page_number}",
                action_label="重新尝试",
            )

        current_page = max(1, min(article.page, article.total_pages))
        if current_page == 1:
            first_post = article.posts[0]
            replies = article.posts[1:]
        else:
            first_post = None
            replies = article.posts
        title = article.title or str(payload.get("title") or "") or first_post.author
        next_url = ""
        if current_page < article.total_pages:
            next_url = f"/a/{token}?{urlencode({'p': current_page + 1})}"
        prev_url = ""
        if current_page > 1:
            prev_url = f"/a/{token}?{urlencode({'p': current_page - 1})}"

        context: dict[str, Any] = {
            "request": request,
            "token": token,
            "article": article,
            "first_post": first_post,
            "title": title,
            "replies": replies,
            "body_length": _plain_text_length(first_post.body_html)
            if first_post
            else 0,
            "cache_age_label": format_cache_age(
                article.fetched_at,
            )
            if cache_age
            else "刚刚",
            "stale": stale,
            "notice": notice,
            "current_page": current_page,
            "prev_url": prev_url,
            "next_url": next_url,
            "public_base": app_settings.public_base,
        }
        response = templates.TemplateResponse(
            request=request,
            name="article.html",
            context=context,
        )
        response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        response.headers["X-Reader-Cache"] = cache_status
        return response

    @application.get("/i/{token}")
    async def image_proxy(token: str) -> Response:
        try:
            payload = loads_typed(token, app_settings.secret, "image")
        except InvalidToken as exc:
            raise HTTPException(status_code=410, detail="image link expired") from exc
        url = str(payload.get("url") or "")
        if not is_allowed_image_url(url):
            raise HTTPException(status_code=400, detail="image host is not allowed")

        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cached_image = image_cache.read(key)
        if (
            cached_image
            and cached_image.age_seconds <= app_settings.image_cache_ttl_seconds
        ):
            return Response(
                cached_image.body,
                media_type=str(cached_image.metadata.get("content_type") or "image/jpeg"),
                headers={
                    "Cache-Control": "private, max-age=86400",
                    "X-Reader-Cache": "HIT",
                },
            )

        manager: BrowserManager | None = app.state.browser
        if not manager:
            raise HTTPException(status_code=503, detail="browser session is not ready")
        try:
            body, content_type = await manager.fetch_image(url)
        except SMTHUnavailable as exc:
            raise HTTPException(status_code=502, detail="image fetch failed") from exc
        if len(body) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="image is too large")
        image_cache.write(key, body, {"content_type": content_type, "url": url})
        return Response(
            body,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Reader-Cache": "MISS",
            },
        )

    @application.get("/login/{token}", response_class=HTMLResponse)
    async def login_with_token(request: Request, token: str) -> Response:
        if _valid_admin_session(request, app_settings):
            return RedirectResponse("/admin?open=1", status_code=303)
        try:
            payload = loads_typed(token, app_settings.secret, "admin-login")
        except InvalidToken:
            return render_message(
                request,
                status_code=410,
                eyebrow="LOGIN LINK EXPIRED",
                title="这个登录入口已失效",
                description="登录入口只能使用一次，并且有效期为 15 分钟。",
            )
        nonce = str(payload.get("jti") or "")
        if not _consume_login_nonce(app_settings, nonce):
            return render_message(
                request,
                status_code=409,
                eyebrow="LOGIN LINK USED",
                title="这个登录入口已经使用过",
                description="请从原来的文章链接重新进入，或让管理员生成新的登录入口。",
            )
        response = RedirectResponse("/admin?open=1", status_code=303)
        _set_session_cookie(response, app_settings)
        return response

    @application.get("/relogin/{token}", response_class=HTMLResponse)
    async def relogin(
        request: Request,
        token: str,
        challenge: int = 0,
        p: int = 1,
    ) -> Response:
        try:
            payload = loads_typed(token, app_settings.secret, "article")
        except InvalidToken:
            return render_message(
                request,
                status_code=410,
                eyebrow="LINK EXPIRED",
                title="阅读链接已失效，无法建立登录会话",
                description="请在飞书里重新获取最新推荐卡片。",
            )
        board = str(payload.get("board") or "")
        topic = str(payload.get("topic") or "")
        page_number = max(1, min(p, 999))
        next_url = _safe_next(f"/a/{token}?{urlencode({'p': page_number})}")
        params = {
            "open": 1,
            "next": next_url,
            "board": board,
            "topic": topic,
            "page": page_number,
        }
        if challenge:
            params["challenge"] = 1
        response = RedirectResponse(
            "/admin?" + urlencode(params),
            status_code=303,
        )
        _set_session_cookie(response, app_settings)
        return response

    @application.get(
        "/admin",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def admin(
        request: Request,
        open: int = 0,
        next: str = "",
        board: str = "",
        topic: str = "",
        page: int = 1,
        challenge: int = 0,
    ) -> HTMLResponse | Response:
        if not _valid_admin_session(request, app_settings):
            return render_message(
                request,
                status_code=401,
                eyebrow="ADMIN SESSION",
                title="需要一次性登录入口",
                description=(
                    "远程浏览器只对持有有效阅读链接或一次性登录入口的人开放。"
                    "请从水木阅读页的“重新登录”按钮进入。"
                ),
            )
        safe_next = _safe_next(next)
        manager: BrowserManager | None = request.app.state.browser
        browser_ready = manager is not None
        opened = False
        target_valid = bool(
            re.fullmatch(r"[A-Za-z0-9_]{1,64}", board)
            and re.fullmatch(r"\d{1,32}", topic)
        )
        page_number = max(1, min(page, 999))
        challenge_mode = bool(target_valid and challenge)
        if manager and open:
            try:
                if challenge_mode:
                    await manager.open_target(board, topic, page_number)
                elif safe_next:
                    status = await manager.session_status(force=False)
                    if status.get("valid"):
                        return RedirectResponse(safe_next, status_code=303)
                    await manager.open_login()
                else:
                    await manager.open_login()
                opened = True
            except Exception:  # noqa: BLE001
                LOGGER.exception("failed to open the SMTH browser target")
        vnc_url = (
            "/admin/vnc/vnc.html?"
            + urlencode(
                {
                    "autoconnect": "true",
                    "reconnect": "true",
                    "resize": "scale",
                    "path": "admin/vnc/websockify",
                    "view_only": "false",
                }
            )
        )
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context={
                "browser_ready": browser_ready,
                "browser_mode": manager.mode if manager else "",
                "browser_error": request.app.state.browser_error,
                "opened": opened,
                "challenge_mode": challenge_mode,
                "target_board": board if target_valid else "",
                "target_topic": topic if target_valid else "",
                "target_page": page_number,
                "vnc_url": vnc_url,
                "next_url": safe_next,
                "session_ttl_label": _format_session_ttl(
                    app_settings.admin_session_ttl_seconds
                ),
            },
        )

    @application.post("/admin/open-login")
    async def open_login(
        request: Request,
        board: str = "",
        topic: str = "",
        page: int = 1,
        challenge: int = 0,
        next: str = "",
    ) -> Response:
        if not _valid_admin_session(request, app_settings):
            raise HTTPException(status_code=401, detail="admin session required")
        manager: BrowserManager | None = request.app.state.browser
        if not manager:
            raise HTTPException(status_code=503, detail="browser is not ready")
        page_number = max(1, min(page, 999))
        target_valid = bool(
            re.fullmatch(r"[A-Za-z0-9_]{1,64}", board)
            and re.fullmatch(r"\d{1,32}", topic)
        )
        safe_next = _safe_next(next)
        try:
            if target_valid and challenge:
                await manager.open_target(board, topic, page_number)
            elif safe_next:
                status = await manager.session_status(force=False)
                if status.get("valid"):
                    return JSONResponse({"ok": True, "redirect": safe_next})
                await manager.open_login()
            else:
                await manager.open_login()
        except SMTHRateLimited as exc:
            raise HTTPException(status_code=502, detail="SMTH is rate limiting") from exc
        return JSONResponse({"ok": True})

    @application.get("/admin/page-status")
    async def page_status(request: Request) -> JSONResponse:
        if not _valid_admin_session(request, app_settings):
            raise HTTPException(status_code=401, detail="admin session required")
        manager: BrowserManager | None = request.app.state.browser
        if not manager:
            return JSONResponse(
                {
                    "challenge": False,
                    "rate_limited": False,
                    "article_ready": False,
                    "logged_in": False,
                    "error": "browser_not_ready",
                },
                status_code=503,
            )
        try:
            result = await manager.page_status()
        except SMTHUnavailable as exc:
            raise HTTPException(status_code=503, detail="page state unavailable") from exc
        return JSONResponse(result)

    @application.get("/admin/session-status")
    async def session_status(request: Request) -> JSONResponse:
        if not _valid_admin_session(request, app_settings):
            raise HTTPException(status_code=401, detail="admin session required")
        manager: BrowserManager | None = request.app.state.browser
        if not manager:
            return JSONResponse(
                {"valid": False, "user": "", "error": "browser_not_ready"}
            )
        result = await manager.session_status(force=False)
        return JSONResponse(result)

    @application.post("/logout")
    async def logout() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @application.websocket("/admin/vnc/websockify")
    async def vnc_websocket(websocket: WebSocket) -> None:
        if not _valid_admin_websocket(websocket, app_settings):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            async with websocket_connect(
                app_settings.vnc_websocket_url,
                open_timeout=10,
                close_timeout=5,
                max_size=None,
            ) as upstream:

                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                tasks = {
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                }
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    try:
                        task.result()
                    except WebSocketDisconnect:
                        LOGGER.info("VNC client websocket disconnected")
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("VNC websocket relay task failed")
        except Exception:  # noqa: BLE001
            LOGGER.exception("VNC websocket proxy failed")
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass

    @application.api_route(
        "/admin/vnc/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def vnc_http_proxy(request: Request, path: str) -> Response:
        if not _valid_admin_session(request, app_settings):
            raise HTTPException(status_code=401, detail="admin session required")
        if not path:
            path = "vnc.html"
        upstream_url = f"{app_settings.vnc_http_url}/{path}"
        if request.url.query:
            upstream_url += f"?{request.url.query}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in VNC_REQUEST_HEADERS
        }
        client = httpx.AsyncClient(follow_redirects=False, timeout=30)
        try:
            upstream_request = client.build_request(
                request.method,
                upstream_url,
                headers=headers,
                content=await request.body(),
            )
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(status_code=502, detail="VNC proxy unavailable") from exc

        response_headers = {
            key: value
            for key, value in upstream_response.headers.items()
            if key.lower() not in VNC_RESPONSE_HEADERS
        }

        async def close_upstream() -> None:
            await upstream_response.aclose()
            await client.aclose()

        return StreamingResponse(
            upstream_response.aiter_bytes(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            background=BackgroundTask(close_upstream),
        )

    @application.get("/")
    async def index(request: Request) -> HTMLResponse:
        return render_message(
            request,
            status_code=200,
            eyebrow="PRIVATE READER",
            title="水木阅读网关",
            description="请从飞书推荐卡片中的文章链接进入。",
        )

    return application


app = create_app()
