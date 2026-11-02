from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page, sync_playwright


BASE_URL = "https://smth-reader.12879737.xyz"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ARTIFACT_DIR = Path(
    r"C:\Users\dduan\.codex\visualizations\2026\10\31"
    r"\01a17343-dc54-73a3-9fbb-67eaf73cbc35"
)


def launch_context(playwright: Any, user_data_dir: Path) -> BrowserContext:
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        headless=True,
        executable_path=CHROME_PATH,
        viewport={"width": 390, "height": 844},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=[
            "--no-proxy-server",
            "--host-resolver-rules=MAP smth-reader.12879737.xyz 104.21.13.38",
        ],
    )


def attach_diagnostics(
    page: Page,
    responses: list[dict[str, Any]],
    console_errors: list[str],
    page_errors: list[str],
) -> None:
    page.on(
        "response",
        lambda response: responses.append(
            {
                "url": response.url,
                "status": response.status,
                "location": response.headers.get("location"),
                "set_cookie": response.headers.get("set-cookie"),
            }
        ),
    )
    page.on(
        "console",
        lambda message: (
            console_errors.append(message.text)
            if message.type == "error"
            else None
        ),
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))


def main() -> None:
    login_url = os.environ["SMTH_TEST_LOGIN_URL"].strip()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(tempfile.mkdtemp(prefix="smth-reader-session-acceptance-"))
    result: dict[str, Any] = {"user_data_dir": str(user_data_dir)}

    try:
        with sync_playwright() as playwright:
            responses: list[dict[str, Any]] = []
            console_errors: list[str] = []
            page_errors: list[str] = []

            context = launch_context(playwright, user_data_dir)
            page = context.pages[0]
            attach_diagnostics(page, responses, console_errors, page_errors)

            health_response = page.goto(
                f"{BASE_URL}/healthz",
                wait_until="domcontentloaded",
            )
            result["health_status"] = (
                health_response.status if health_response else None
            )

            anonymous_response = page.goto(
                f"{BASE_URL}/admin",
                wait_until="domcontentloaded",
            )
            result["anonymous_admin_status"] = (
                anonymous_response.status if anonymous_response else None
            )

            page.goto(login_url, wait_until="domcontentloaded")
            page.wait_for_url("**/admin?open=1", timeout=45_000)
            page.wait_for_load_state("domcontentloaded")
            result["login_final_url"] = page.url
            result["admin_title"] = page.title()

            login_responses = [
                response
                for response in responses
                if "/login/" in response["url"]
            ]
            result["login_responses"] = login_responses
            result["admin_cookie_headers"] = [
                response["set_cookie"]
                for response in login_responses
                if response["set_cookie"]
            ]

            admin_cookie = next(
                (
                    cookie
                    for cookie in context.cookies(BASE_URL)
                    if cookie["name"] == "smth_reader_admin"
                ),
                None,
            )
            if admin_cookie:
                expires_at = datetime.fromtimestamp(
                    admin_cookie["expires"],
                    tz=timezone.utc,
                )
                remaining_days = (
                    expires_at - datetime.now(tz=timezone.utc)
                ).total_seconds() / 86400
                result["admin_cookie"] = {
                    "http_only": admin_cookie["httpOnly"],
                    "secure": admin_cookie["secure"],
                    "same_site": admin_cookie["sameSite"],
                    "path": admin_cookie["path"],
                    "expires_utc": expires_at.isoformat(),
                    "remaining_days": round(remaining_days, 2),
                }
            else:
                result["admin_cookie"] = None

            page.screenshot(
                path=str(ARTIFACT_DIR / "smth-reader-admin-session-mobile.png"),
                full_page=True,
            )
            result["not_found_urls"] = [
                response["url"]
                for response in responses
                if response["status"] == 404
            ]
            result["console_errors"] = console_errors
            result["page_errors"] = page_errors
            context.close()

            reopened_responses: list[dict[str, Any]] = []
            reopened_console_errors: list[str] = []
            reopened_page_errors: list[str] = []
            reopened = launch_context(playwright, user_data_dir)
            reopened_page = reopened.pages[0]
            attach_diagnostics(
                reopened_page,
                reopened_responses,
                reopened_console_errors,
                reopened_page_errors,
            )

            reopened_admin = reopened_page.goto(
                f"{BASE_URL}/admin",
                wait_until="domcontentloaded",
            )
            result["reopened_admin_status"] = (
                reopened_admin.status if reopened_admin else None
            )
            result["reopened_admin_title"] = reopened_page.title()
            result["reopened_cookie_present"] = any(
                cookie["name"] == "smth_reader_admin"
                for cookie in reopened.cookies(BASE_URL)
            )

            logout_status = reopened_page.evaluate(
                """async () => {
                    const response = await fetch("/logout", {
                        method: "POST",
                        credentials: "same-origin",
                    });
                    return response.status;
                }"""
            )
            result["logout_status"] = logout_status

            logged_out = reopened_page.goto(
                f"{BASE_URL}/admin",
                wait_until="domcontentloaded",
            )
            result["logged_out_admin_status"] = (
                logged_out.status if logged_out else None
            )
            result["reopened_console_errors"] = reopened_console_errors
            result["reopened_page_errors"] = reopened_page_errors
            reopened.close()
    finally:
        shutil.rmtree(user_data_dir, ignore_errors=True)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
