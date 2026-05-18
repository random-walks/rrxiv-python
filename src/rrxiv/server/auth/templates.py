"""HTML templates for the auth render endpoints.

Inline strings rather than a Jinja directory: keeps the package
self-contained and lets the templates participate in unit tests
without filesystem fixtures.
"""

from __future__ import annotations

from html import escape

ORCID_PASTE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>rrxiv login — paste code</title>
  <style>
    body {{
      font-family: ui-sans-serif, system-ui, sans-serif;
      max-width: 540px;
      margin: 4rem auto;
      padding: 0 1rem;
      color: #222;
    }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
    p {{ line-height: 1.5; color: #444; }}
    .code {{
      display: flex;
      gap: 0.5rem;
      align-items: center;
      margin: 1.5rem 0;
    }}
    .code code {{
      font-size: 1.4rem;
      padding: 0.5rem 0.75rem;
      background: #f3f4f6;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      letter-spacing: 0.05em;
      flex: 1;
    }}
    button {{
      padding: 0.5rem 1rem;
      background: #2563eb;
      color: white;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.95rem;
    }}
    button:hover {{ background: #1d4ed8; }}
    .meta {{ color: #6b7280; font-size: 0.85rem; margin-top: 2rem; }}
  </style>
</head>
<body>
  <h1>rrxiv login — copy this code</h1>
  <p>
    Paste this code into your terminal where <code>rrxiv login orcid
    --no-browser</code> is waiting:
  </p>
  <div class="code">
    <code id="paste-code">{code}</code>
    <button onclick="copyCode()">Copy</button>
  </div>
  <p class="meta">
    Linked to ORCID iD <code>{orcid_id}</code>. Single-use; expires in
    {expires_in_minutes} minutes.
  </p>
  <script>
    function copyCode() {{
      const t = document.getElementById('paste-code').textContent;
      navigator.clipboard.writeText(t);
      event.target.textContent = 'Copied';
    }}
  </script>
</body>
</html>
"""

ANONYMOUS_HCAPTCHA_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>rrxiv anonymous attestation</title>
  <script src="https://js.hcaptcha.com/1/api.js" async defer></script>
  <style>
    body {{
      font-family: ui-sans-serif, system-ui, sans-serif;
      max-width: 540px;
      margin: 4rem auto;
      padding: 0 1rem;
      color: #222;
    }}
    h1 {{ font-size: 1.4rem; margin-bottom: 0.5rem; }}
    p {{ line-height: 1.5; color: #444; }}
    .response {{
      display: none;
      margin: 1.5rem 0;
      flex-direction: column;
      gap: 0.5rem;
    }}
    .response.shown {{ display: flex; }}
    textarea {{
      width: 100%;
      min-height: 90px;
      font-family: ui-monospace, monospace;
      font-size: 0.85rem;
      padding: 0.5rem;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      background: #f9fafb;
    }}
    button {{
      padding: 0.5rem 1rem;
      background: #2563eb;
      color: white;
      border: 0;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.95rem;
      align-self: flex-start;
    }}
    button:hover {{ background: #1d4ed8; }}
    .meta {{ color: #6b7280; font-size: 0.85rem; margin-top: 2rem; }}
  </style>
</head>
<body>
  <h1>rrxiv anonymous attestation</h1>
  <p>
    Solve the challenge below. The resulting token is what you paste
    into your terminal where <code>rrxiv login anonymous</code> is
    waiting.
  </p>
  <div class="h-captcha"
       data-sitekey="{site_key}"
       data-callback="onSolved"></div>
  <div class="response" id="response">
    <textarea id="response-text" readonly></textarea>
    <button onclick="copyResponse()">Copy token</button>
  </div>
  <p class="meta">
    Challenge ID: <code>{challenge_id}</code>
  </p>
  <script>
    function onSolved(token) {{
      document.getElementById('response-text').value = token;
      document.getElementById('response').classList.add('shown');
    }}
    function copyResponse() {{
      const t = document.getElementById('response-text');
      t.select();
      navigator.clipboard.writeText(t.value);
    }}
  </script>
</body>
</html>
"""


def render_orcid_paste(
    *, code: str, orcid_id: str, expires_in_minutes: int
) -> str:
    return ORCID_PASTE_TEMPLATE.format(
        code=escape(code),
        orcid_id=escape(orcid_id),
        expires_in_minutes=expires_in_minutes,
    )


def render_anonymous_hcaptcha(*, site_key: str, challenge_id: str) -> str:
    return ANONYMOUS_HCAPTCHA_TEMPLATE.format(
        site_key=escape(site_key),
        challenge_id=escape(challenge_id),
    )


__all__ = ["render_anonymous_hcaptcha", "render_orcid_paste"]
