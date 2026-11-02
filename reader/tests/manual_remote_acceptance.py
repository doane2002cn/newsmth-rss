from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE_URL = "https://smth-reader.12879737.xyz"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ARTIFACT_DIR = Path(
    r"C:\Users\dduan\.codex\visualizations\2026\10\25"
    r"\01a17343-dc54-73a3-9fbb-67eaf73cbc35"
)


def main() -> None:
    login_url = os.environ["SMTH_TEST_LOGIN_URL"].strip()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=CHROME_PATH,
            args=[
                "--no-proxy-server",
                "--host-resolver-rules=MAP smth-reader.12879737.xyz 104.21.13.38",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = context.new_page()
        page_errors: list[str] = []
        console_errors: list[str] = []
        console_messages: list[dict[str, str]] = []
        websocket_frames: list[dict[str, object]] = []
        cdp = context.new_cdp_session(page)
        cdp.send("Network.enable")
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "console",
            lambda message: console_messages.append(
                {"type": message.type, "text": message.text}
            ),
        )
        cdp.on(
            "Network.webSocketFrameReceived",
            lambda event: websocket_frames.append(
                {
                    "direction": "received",
                    "opcode": event["response"]["opcode"],
                    "length": event["response"].get("payloadLength"),
                }
            ),
        )
        cdp.on(
            "Network.webSocketFrameSent",
            lambda event: websocket_frames.append(
                {
                    "direction": "sent",
                    "opcode": event["response"]["opcode"],
                    "length": event["response"].get("payloadLength"),
                }
            ),
        )

        health = page.goto(f"{BASE_URL}/healthz", wait_until="domcontentloaded")
        result["health_status"] = health.status if health else None
        result["health"] = page.evaluate(
            """() => {
                try { return JSON.parse(document.body.innerText); }
                catch { return document.body.innerText; }
            }"""
        )

        anonymous = browser.new_context(
            viewport={"width": 1200, "height": 800},
        ).new_page()
        result["anonymous_admin_status"] = anonymous.goto(
            f"{BASE_URL}/admin",
            wait_until="domcontentloaded",
        ).status
        anonymous.context.close()

        login_response = page.goto(login_url, wait_until="domcontentloaded")
        result["login_status"] = login_response.status if login_response else None
        page.wait_for_url("**/admin?open=1", timeout=45_000)
        page.wait_for_load_state("domcontentloaded")
        result["final_url"] = page.url
        result["admin_heading"] = page.title()
        result["login_alert"] = page.locator(".admin-alert").inner_text()

        cookies = context.cookies()
        admin_cookie = next(
            (cookie for cookie in cookies if cookie["name"] == "smth_reader_admin"),
            None,
        )
        result["admin_cookie"] = (
            {
                "http_only": admin_cookie["httpOnly"],
                "secure": admin_cookie["secure"],
                "same_site": admin_cookie["sameSite"],
                "path": admin_cookie["path"],
                "has_domain": bool(admin_cookie["domain"]),
            }
            if admin_cookie
            else None
        )

        frame_element = page.locator("#vncFrame")
        frame_element.wait_for(state="visible", timeout=20_000)
        frame_handle = frame_element.element_handle()
        frame = frame_handle.content_frame() if frame_handle else None
        if frame is None:
            raise RuntimeError("noVNC iframe did not expose its content frame")
        frame.wait_for_selector(
            "#noVNC_container canvas",
            state="visible",
            timeout=45_000,
        )

        deadline = time.monotonic() + 30
        status_text = ""
        while time.monotonic() < deadline:
            status = frame.locator("#noVNC_status")
            if status.count():
                status_text = status.first.inner_text().strip()
                if status_text and not status.first.is_hidden():
                    break
            page.wait_for_timeout(500)

        canvas = frame.locator("#noVNC_container canvas")
        result["vnc_status"] = status_text
        page.wait_for_timeout(5000)
        result["canvas"] = canvas.evaluate(
            """canvas => ({
                width: canvas.width,
                height: canvas.height,
                cssWidth: canvas.getBoundingClientRect().width,
                cssHeight: canvas.getBoundingClientRect().height,
            })"""
        )
        result["canvas_samples"] = canvas.evaluate(
            """canvas => {
                const context = canvas.getContext("2d");
                const colors = new Set();
                for (let y = 0; y < canvas.height; y += 40) {
                    for (let x = 0; x < canvas.width; x += 40) {
                        const pixel = context.getImageData(x, y, 1, 1).data;
                        colors.add(`${pixel[0]},${pixel[1]},${pixel[2]},${pixel[3]}`);
                    }
                }
                return { uniqueColors: colors.size, colors: [...colors].slice(0, 12) };
            }"""
        )
        result["iframe_url"] = frame.url
        result["vnc_page_title"] = frame.title()
        result["websocket_probe"] = frame.evaluate(
            """() => {
                const rfb = window.rfb || window.UI?.rfb;
                return {
                    connected: Boolean(rfb),
                    state: rfb?._rfbConnectionState ?? null,
                    viewOnly: Boolean(rfb?.viewOnly),
                };
            }"""
        )

        page.screenshot(
            path=str(ARTIFACT_DIR / "smth-reader-admin-desktop.png"),
            full_page=True,
        )
        canvas.screenshot(
            path=str(ARTIFACT_DIR / "smth-reader-vnc-canvas.png"),
        )

        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1000)
        result["mobile_layout"] = page.evaluate(
            """() => ({
                viewportWidth: window.innerWidth,
                documentWidth: document.documentElement.scrollWidth,
                bodyWidth: document.body.scrollWidth,
                frameWidth: document.querySelector("#vncFrame")?.getBoundingClientRect().width,
                visibleActionLabels: [...document.querySelectorAll(".admin-button span")]
                    .filter(element => getComputedStyle(element).display !== "none").length,
            })"""
        )
        page.screenshot(
            path=str(ARTIFACT_DIR / "smth-reader-admin-mobile.png"),
            full_page=True,
        )

        result["page_errors"] = page_errors
        result["console_errors"] = console_errors
        result["console_messages"] = console_messages
        result["websocket_frames"] = websocket_frames[:40]
        result["websocket_frame_count"] = len(websocket_frames)
        result["screenshots"] = [
            str(ARTIFACT_DIR / "smth-reader-admin-desktop.png"),
            str(ARTIFACT_DIR / "smth-reader-admin-mobile.png"),
            str(ARTIFACT_DIR / "smth-reader-vnc-canvas.png"),
        ]
        browser.close()

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
