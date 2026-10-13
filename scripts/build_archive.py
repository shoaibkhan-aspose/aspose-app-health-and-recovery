"""Publish the report archive next to the latest report (GitHub Pages).

    python scripts/build_archive.py --out site

Snapshots are frozen, standalone report pages in reports/archive/ (never rebuilt), listed in
reports/archive/index.json. Each is served at <out>/archive/<date>/, with a list at <out>/archive/.
New snapshots come from `build_report.py --snapshot "<label>"`.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = REPO_ROOT / "reports" / "archive"

PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Report archive</title>
<style>
:root { --bg: #f6f7f9; --surface: #fff; --fg: #18202b; --muted: #5b6676; --line: #dde2e9; --accent: #1f5f8b }
@media (prefers-color-scheme: dark) { :root { --bg: #11161d; --surface: #182029; --fg: #e4e9f0; --muted: #98a3b3;
  --line: #2a3542; --accent: #6fb0dc; color-scheme: dark } }
* { box-sizing: border-box }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif }
main { max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; display: grid; gap: 20px }
h1 { margin: 0; font-size: 28px } p { margin: 0; color: var(--muted) }
a { color: var(--accent) }
ol { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px }
li { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px;
  display: grid; gap: 2px }
li b { font-size: 16px } li span { color: var(--muted); font-size: 14px }
</style>
<main>
  <h1>Report archive</h1>
  <p>Frozen copies of the aspose.app site health report. Each copy shows the report exactly as it was on that date.
  <a href="../">Back to the latest report</a></p>
  <ol>
__ITEMS__
  </ol>
</main>
</html>
"""


MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def readable_day(value: str) -> str:
    """'2026-10-12' -> '12 Oct 2026'; a suffix such as ' (2)' is kept."""
    head, _, rest = value.partition(" ")
    try:
        y, m, d = (int(x) for x in head.split("-"))
    except ValueError:
        return value
    return f"{d} {MONTHS[m - 1]} {y}" + (f" {rest}" if rest else "")


def entries() -> list[dict]:
    path = ARCHIVE / "index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def build(out: Path) -> int:
    items = sorted(entries(), key=lambda e: e["date"], reverse=True)
    target = out / "archive"
    target.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in items:
        slug = Path(e["file"]).stem
        (target / slug).mkdir(exist_ok=True)
        shutil.copyfile(ARCHIVE / e["file"], target / slug / "index.html")
        rows.append(f'    <li><a href="{html.escape(slug)}/"><b>{html.escape(readable_day(e["date"]))}</b></a>'
                    f'<span>{html.escape(e.get("label", ""))}{" · " + html.escape(e["runs"]) if e.get("runs") else ""}'
                    f"</span></li>")
    (target / "index.html").write_text(PAGE.replace("__ITEMS__", "\n".join(rows)), encoding="utf-8")
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="site", help="Pages output folder")
    args = ap.parse_args(argv)
    n = build(Path(args.out))
    print(f"Archive: {n} snapshots in {Path(args.out) / 'archive'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
