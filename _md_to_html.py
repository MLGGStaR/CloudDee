"""Render the submission markdown to a print-ready HTML page.
Open YOUTUBE_API_SUBMISSION.html in any browser, then Ctrl+P -> Save as PDF.
"""
from pathlib import Path

import markdown

SRC = Path("YOUTUBE_API_SUBMISSION.md")
DST = Path("YOUTUBE_API_SUBMISSION.html")

md = SRC.read_text(encoding="utf-8")
body = markdown.markdown(md, extensions=["tables", "fenced_code"])

html = """<!doctype html>
<html><head><meta charset='utf-8'>
<title>CloudDee — YouTube API Submission</title>
<style>
@page { size: A4; margin: 1.6cm 2cm; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.5; color: #111;
       max-width: 780px; margin: 24px auto; padding: 0 24px; }
h1 { font-size: 20pt; border-bottom: 2px solid #0c2d48; padding-bottom: 6px; margin-top: 0; }
h2 { font-size: 14pt; color: #0c2d48; margin-top: 24px; }
h3 { font-size: 12pt; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; }
th, td { border: 1px solid #ccc; padding: 6px 10px; font-size: 10pt; text-align: left; }
th { background: #f3f4f6; }
code, pre { background: #f6f8fa; font-family: "Consolas", "Monaco", monospace; font-size: 9.5pt; }
pre { padding: 10px; border-radius: 4px; white-space: pre-wrap; overflow-x: auto; }
blockquote { border-left: 3px solid #0c2d48; padding-left: 12px; color: #444; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 14px 0; }
@media print { body { max-width: none; margin: 0; padding: 0; } }
</style></head>
<body>
""" + body + """
</body></html>
"""

DST.write_text(html, encoding="utf-8")
print(f"HTML written: {DST.resolve()}")
print("Open it in any browser, then Ctrl+P → 'Save as PDF'.")
