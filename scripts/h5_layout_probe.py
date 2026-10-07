"""Capture the local H5 module layout with the installed Edge browser."""

from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    output = Path("F:/Temp/fde-h5-layout")
    output.mkdir(parents=True, exist_ok=True)
    browser_path = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=browser_path, headless=True)
        for name, width, height in (("desktop", 1440, 900), ("mobile", 390, 844), ("narrow", 320, 700)):
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
            page.goto("http://127.0.0.1:8211/preview-login", wait_until="networkidle")
            page.locator(".todo").first.wait_for()
            for view in ("meetings", "owner-tasks", "organizer-tasks", "follow-up", "agents"):
                page.locator(f"#module-{view}").click()
                page.wait_for_timeout(150)
                visible = page.locator(".view-panel:visible").count()
                overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth")
                selected = page.locator("#module-nav [aria-selected=true]").get_attribute("data-view")
                labels_fit = page.locator("#module-nav button").evaluate_all(
                    "buttons => buttons.every(button => button.scrollWidth <= button.clientWidth + 1)"
                )
                print(f"{name} {view} visible_panels={visible} horizontal_overflow={overflow}")
                assert visible == 1 and not overflow and selected == view and labels_fit
                page.screenshot(path=str(output / f"{name}-{view}.png"), full_page=True)
            page.close()

        page = browser.new_page()
        page.goto("http://127.0.0.1:8211/preview-login#owner-tasks", wait_until="networkidle")
        assert page.locator("#view-owner-tasks").is_visible()
        page.locator("#module-meetings").click()
        page.locator(".todo-title").first.fill("未保存的会议草稿")
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#module-owner-tasks").click()
        assert page.locator("#view-meetings").is_visible()
        assert page.locator(".todo-title").first.input_value() == "未保存的会议草稿"
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#module-owner-tasks").click()
        assert page.locator("#view-owner-tasks").is_visible()
        page.go_back()
        assert page.locator("#view-meetings").is_visible()
        page.close()
        browser.close()


if __name__ == "__main__":
    main()
