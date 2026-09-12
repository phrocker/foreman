"""Screenshot every dashboard tab, in both themes, and fail on console errors.

Written after shipping the dashboard twice with a ReferenceError that blanked
three of four panels. `node --check` cannot catch an undefined variable inside a
template literal, and the API was returning correct data the whole time — the
only thing that would have caught it was looking at the page.

    foreman serve --port 8768 &
    python scripts/screenshot.py --port 8768 --out /tmp/shots
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

TABS = ("findings", "actions", "drift", "history", "memory", "rules")


async def shoot(port: int, out: Path) -> int:
    from playwright.async_api import async_playwright

    out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        for scheme in ("light", "dark"):
            page = await browser.new_page(
                viewport={"width": 1180, "height": 1500}, color_scheme=scheme
            )
            page.on("console", lambda m: m.type == "error" and problems.append(m.text))
            page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
            await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            await page.wait_for_timeout(900)
            for tab in TABS:
                await page.click(f'.tab[data-tab="{tab}"]')
                await page.wait_for_timeout(350)
                await page.screenshot(path=str(out / f"ui-{tab}-{scheme}.png"))
            await page.close()
        await browser.close()

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    print(f"{len(TABS) * 2} screenshots in {out}")
    return 1 if problems else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, default=Path("screenshots"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(shoot(args.port, args.out)))


if __name__ == "__main__":
    main()
