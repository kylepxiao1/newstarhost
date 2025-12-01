"""
Export YouTube cookies from installed browsers to cookies.txt (Netscape format).
Requires: pip install -r requirements.txt (browser-cookie3)
Usage:
  python scripts/export_cookies.py
Output:
  cookies.txt in repo root
Note: only run on your own machine/session; the file contains auth cookies.
"""

import http.cookiejar
from pathlib import Path

import browser_cookie3

OUTPUT = Path(__file__).resolve().parent.parent / "cookies.txt"


def export() -> None:
    cj = http.cookiejar.MozillaCookieJar()

    for loader in (
        browser_cookie3.chrome,
        browser_cookie3.chromium,
        browser_cookie3.edge,
        browser_cookie3.firefox,
    ):
        try:
            cookies = loader(domain_name="youtube.com")
            for c in cookies:
                cj.set_cookie(c)
        except Exception:
            continue

    cj.save(str(OUTPUT), ignore_discard=True, ignore_expires=True)
    print(f"Cookies written to {OUTPUT}")
    print("Set env for yt-dlp:")
    print(f'  $env:YTDLP_COOKIES="{OUTPUT}"')


if __name__ == "__main__":
    export()
