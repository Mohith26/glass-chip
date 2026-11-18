#!/usr/bin/env python3
"""Render report/report.md (or docs/one-pager.md) to a self-contained-looking HTML page for printing.

Usage: python report/build_html.py report/report.md report/report.html
       python report/build_html.py docs/one-pager.md docs/one-pager.html docs/one-pager.css
Needs the `markdown` package (pip install markdown). I print the HTML to PDF from Chrome
(page.pdf with printBackground), which keeps fonts and figure placement identical to the screen.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown

CSS = """
@page { size: Letter; margin: 16mm 17mm 17mm 17mm; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: "Charter", "Iowan Old Style", "Georgia", serif; font-size: 10pt; line-height: 1.37;
       color: #111; max-width: 7.2in; margin: 0 auto; }
h1 { font-family: "Helvetica Neue", "Inter", Arial, sans-serif; font-size: 17pt; line-height: 1.2;
     margin: 0 0 4pt; font-weight: 700; letter-spacing: -0.2pt; }
h2 { font-family: "Helvetica Neue", "Inter", Arial, sans-serif; font-size: 12pt; margin: 13pt 0 4pt;
     font-weight: 700; break-after: avoid; }
h3 { font-family: "Helvetica Neue", "Inter", Arial, sans-serif; font-size: 10.5pt; margin: 9pt 0 3pt;
     font-weight: 700; break-after: avoid; }
p { margin: 0 0 6pt; text-align: justify; hyphens: auto; }
ul { margin: 0 0 6pt; padding-left: 16pt; } li { margin: 0 0 2pt; }
code { font-family: "SF Mono", Menlo, monospace; font-size: 8.4pt; background: #f2f2f2; padding: 0 2pt;
       border-radius: 2pt; }
pre { font-family: "SF Mono", Menlo, monospace; font-size: 7.4pt; line-height: 1.3; background: #f6f6f6;
      border: 0.5pt solid #ddd; padding: 6pt 8pt; border-radius: 3pt; overflow: hidden; break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0 8pt; font-size: 8.6pt; line-height: 1.3;
        break-inside: avoid; }
th, td { border-bottom: 0.5pt solid #bbb; padding: 2.5pt 5pt; text-align: left; vertical-align: top; }
th { border-bottom: 1pt solid #333; font-family: "Helvetica Neue", Arial, sans-serif; font-weight: 600; }
figure { margin: 6pt 0 10pt; break-inside: avoid; text-align: center; }
figure img { max-width: 100%; max-height: 2.5in; }
figure.pair img { width: 49%; max-height: 2.4in; object-fit: contain; vertical-align: top; }
figcaption { font-size: 8.4pt; line-height: 1.3; color: #333; text-align: left; margin-top: 3pt; }
.byline { font-family: "Helvetica Neue", Arial, sans-serif; font-size: 10pt; margin-bottom: 6pt; }
em { color: #222; }
"""


def render(md_path: Path, out_path: Path, extra_css: str = "") -> None:
    text = md_path.read_text(encoding="utf-8")
    text = re.sub(r"[ \t]*<!--.*?-->", "", text, flags=re.S)  # source comments are for numcheck, not readers
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    title = re.search(r"^# (.+)$", text, re.M)
    html = (f"<!doctype html><html><head><meta charset='utf-8'><title>{title.group(1) if title else ''}</title>"
            f"<style>{CSS}{extra_css}</style></head><body>{body}</body></html>")
    out_path.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    css = Path(sys.argv[3]).read_text() if len(sys.argv) > 3 else ""
    render(Path(sys.argv[1]), Path(sys.argv[2]), css)
