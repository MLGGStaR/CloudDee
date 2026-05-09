"""Convert YOUTUBE_API_SUBMISSION.md to a PDF for the YouTube quota form."""
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

SRC = Path("YOUTUBE_API_SUBMISSION.md")
DST = Path("YOUTUBE_API_SUBMISSION.pdf")

md = SRC.read_text(encoding="utf-8")
html_body = markdown.markdown(md, extensions=["tables", "fenced_code"])

html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>CloudDee YouTube API Submission</title></head>
<body>{html_body}</body></html>"""

css = CSS(string="""
@page { size: A4; margin: 1.5cm 2cm; }
body { font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
       font-size: 10.5pt; line-height: 1.45; color: #111; }
h1 { font-size: 18pt; border-bottom: 2px solid #0c2d48; padding-bottom: 4px; }
h2 { font-size: 13pt; color: #0c2d48; margin-top: 18px; }
h3 { font-size: 11pt; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { border: 1px solid #ccc; padding: 4px 8px; font-size: 9.5pt; text-align: left; }
th { background: #f3f4f6; }
code, pre { background: #f6f8fa; font-family: "Consolas", "Monaco", monospace;
            font-size: 9pt; }
pre { padding: 8px; border-radius: 4px; white-space: pre-wrap; }
blockquote { border-left: 3px solid #0c2d48; padding-left: 10px; color: #444; }
hr { border: 0; border-top: 1px solid #e5e7eb; margin: 12px 0; }
""")

HTML(string=html_doc).write_pdf(DST, stylesheets=[css])
print(f"PDF: {DST.resolve()}  size={DST.stat().st_size} bytes")
