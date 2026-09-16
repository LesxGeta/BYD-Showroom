#!/usr/bin/env python3
"""
BYD Virtual Showroom - local server.

Serves the showroom, proxies chat to the Anthropic API (so the key never
reaches the browser), and records leads + session analytics to disk.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...        # macOS / Linux
    $env:ANTHROPIC_API_KEY="sk-ant-..."        # Windows PowerShell

    python3 server.py                          # then open http://localhost:8000

Written to ./data/ as you use it:
    leads.jsonl      one JSON object per captured lead
    sessions.jsonl   one JSON object per session
"""

import datetime
import http.server
import json
import os
import re
import socketserver
import sys
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr
import urllib.error
import urllib.parse
import urllib.request



# ------------------------------------------------------------------
#  CONFIGURATION
#
#  Every secret is read from the environment. Nothing is hard-coded,
#  so this file is safe to commit.
#
#  Local: copy .env.example to .env and fill it in. This server reads
#  it automatically at startup, and .env is gitignored.
# ------------------------------------------------------------------


def load_env_file():
    """Read KEY=value pairs from a .env beside this script.

    Real environment variables always win, so a deployment can override
    anything here. Tolerates what Windows editors produce: a BOM, CRLF
    endings, quoted values, and set/export/$env: prefixes."""
    here = os.path.dirname(os.path.abspath(__file__))
    found = [os.path.join(here, n) for n in
             (".env", ".env.txt", "env", "env.txt")
             if os.path.exists(os.path.join(here, n))]
    if not found:
        return None

    names, loaded = [], []
    for path in found:
        names.append(os.path.basename(path))
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.replace("\ufeff", "").replace("\x00", "").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                for prefix in ("set ", "export "):
                    if line.lower().startswith(prefix):
                        line = line[len(prefix):]
                k, v = line.split("=", 1)
                k = k.strip().lstrip("$").replace("env:", "")
                v = v.strip().strip('"').strip("'").strip()
                if k and k not in os.environ:
                    os.environ[k] = v
                    loaded.append(k)
    return (names, loaded)


_ENV_RESULT = load_env_file()

PORT = int(os.environ.get("PORT", "8000"))
# 0.0.0.0 means "listen on all interfaces". Override with HOST=127.0.0.1
# if you specifically want it unreachable from the local network.
HOST = os.environ.get("HOST", "0.0.0.0")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
# Static files are served from public/ when that folder exists, and from
# the repository root otherwise. This keeps both layouts working: a tidy
# repo with public/, and a flat one with index.html at the top level.
PUBLIC = os.path.join(ROOT, "public")
if not os.path.exists(os.path.join(PUBLIC, "index.html")):
    if os.path.exists(os.path.join(ROOT, "index.html")):
        PUBLIC = ROOT

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip()
PROVIDER = os.environ.get("AI_PROVIDER", "auto").strip().lower()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
# Google shows app passwords in groups of four; the real value has no spaces.
SMTP_PASS = os.environ.get("SMTP_PASS", "").replace(" ", "").strip()
MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "BYD South Africa").strip()
MAIL_BCC = os.environ.get("MAIL_BCC", "").strip()

# Remembered once Google accepts one, so we don't re-probe on every call.
_gemini_auth_style = None
_model_tried = set()


def active_provider():
    if PROVIDER == "anthropic":
        return "anthropic" if ANTHROPIC_KEY else None
    if PROVIDER == "gemini":
        return "gemini" if GEMINI_KEY else None
    if GEMINI_KEY:
        return "gemini"
    if ANTHROPIC_KEY:
        return "anthropic"
    return None


def active_model():
    p = active_provider()
    return ANTHROPIC_MODEL if p == "anthropic" else (GEMINI_MODEL if p == "gemini" else "")


def preview(key):
    if not key:
        return ""
    return (key[:10] + "…" + key[-4:]) if len(key) > 18 else "(short)"


# ------------------------------------------------------------------
#  EMAIL
# ------------------------------------------------------------------

SPEC_ROWS = {
    "BYD Seal": [
        ("Battery", "82.56 kWh BYD Blade"),
        ("Range", "570 km WLTP / 650 km NEDC"),
        ("Power", "230 kW / 360 N\u00b7m"),
        ("0\u2013100 km/h", "5.9 seconds"),
        ("DC charging", "150 kW"),
        ("Luggage", "400 L rear + 50 L front"),
        ("Drag coefficient", "0.219 Cd"),
    ],
    "BYD Sealion 7": [
        ("Battery", "82.56 kWh BYD Blade"),
        ("Range", "482 km WLTP / 567 km NEDC"),
        ("Power", "230 kW / 380 N\u00b7m"),
        ("0\u2013100 km/h", "6.7 seconds"),
        ("DC charging", "150 kW"),
        ("Luggage", "500 L rear + 58 L front"),
        ("Consumption", "19.8 kWh/100 km"),
    ],
}

PRICES = {"BYD Seal": "R1 007 900", "BYD Sealion 7": "R1 109 900"}

BROCHURES = {
    "BYD Seal": "brochures/BYD-Seal-Specifications-SA.pdf",
    "BYD Sealion 7": "brochures/BYD-Sealion-7-Specifications-SA.pdf",
}

STOP_LABELS = {
    "stance": "First look", "charge": "Charging", "v2l": "Loadshedding and V2L",
    "battery": "The Blade Battery", "practical": "Everyday practicality",
    "trust": "The brand", "close": "Next steps",
}


def esc(t):
    return (str(t).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_email_html(lead):
    car = lead.get("car", "BYD")
    rows = SPEC_ROWS.get(car, [])
    name = (lead.get("name") or "").strip().split(" ")[0] or "there"
    covered = [STOP_LABELS.get(s, s) for s in lead.get("stopsVisited", [])]
    questions = [q for q in lead.get("questions", []) if q][-8:]

    spec_html = "".join(
        '<tr><td style="padding:9px 0;border-bottom:1px solid #E8EAEB;color:#6B7276;'
        'font-size:13px">%s</td>'
        '<td style="padding:9px 0;border-bottom:1px solid #E8EAEB;text-align:right;'
        'font-weight:600;font-size:13px;color:#252728">%s</td></tr>' % (esc(k), esc(v))
        for k, v in rows)

    covered_html = ""
    if covered:
        covered_html = (
            '<p style="margin:0 0 8px;font-size:13px;color:#6B7276">What we went through</p>'
            '<p style="margin:0 0 24px;font-size:14px;color:#252728;line-height:1.7">'
            + esc(" \u00b7 ".join(covered)) + "</p>")

    questions_html = ""
    if questions:
        questions_html = (
            '<p style="margin:0 0 8px;font-size:13px;color:#6B7276">You asked about</p>'
            '<ul style="margin:0 0 24px;padding-left:18px;font-size:14px;color:#252728;'
            'line-height:1.8">'
            + "".join("<li>%s</li>" % esc(q) for q in questions) + "</ul>")

    return """<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#F4F6F7;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif">
<table width="100%%" cellpadding="0" cellspacing="0" style="background:#F4F6F7;padding:28px 12px">
<tr><td align="center">
<table width="100%%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#FFFFFF;border-radius:6px;overflow:hidden;border:1px solid #E2E5E7">
  <tr><td style="background:#252728;padding:26px 30px">
    <div style="font-size:22px;font-weight:700;color:#FFFFFF;letter-spacing:3px">BYD</div>
    <div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#40A2E3;margin-top:6px">South Africa</div>
  </td></tr>
  <tr><td style="padding:30px">
    <p style="margin:0 0 6px;font-size:20px;font-weight:600;color:#252728">Hi %(name)s, here is your %(car)s summary</p>
    <p style="margin:0 0 26px;font-size:14px;color:#6B7276;line-height:1.7">Thanks for spending time in the virtual showroom. Everything we covered is below, and the full specification sheet is attached.</p>
    <table width="100%%" cellpadding="0" cellspacing="0" style="background:#F7F9FA;border-radius:5px">
      <tr><td style="padding:18px 20px">
        <table width="100%%" cellpadding="0" cellspacing="0">
          <tr><td style="font-size:17px;font-weight:700;color:#252728">%(car)s</td>
              <td style="text-align:right;font-size:16px;font-weight:600;color:#40A2E3">%(price)s</td></tr>
        </table>
        <table width="100%%" cellpadding="0" cellspacing="0" style="margin-top:12px">%(specs)s</table>
      </td></tr>
    </table>
    <div style="height:26px"></div>
    %(covered)s
    %(questions)s
    <table width="100%%" cellpadding="0" cellspacing="0" style="background:#EEF7FD;border-left:3px solid #40A2E3;border-radius:3px">
      <tr><td style="padding:16px 18px">
        <p style="margin:0 0 4px;font-size:14px;font-weight:600;color:#252728">Your nearest dealer: %(dealer)s</p>
        <p style="margin:0;font-size:13px;color:#6B7276;line-height:1.6">The team will be in touch about next steps. Every BYD includes a five-year maintenance plan, an eight-year battery warranty and a home wallbox.</p>
      </td></tr>
    </table>
    <p style="margin:26px 0 0;font-size:12px;color:#9AA1A5;line-height:1.6">Specifications from official BYD South Africa material. BYD reserves the right to vary specifications and standard features. Range figures are WLTP estimates; real-world range depends on driving style, load and conditions.</p>
  </td></tr>
  <tr><td style="background:#F7F9FA;padding:18px 30px;border-top:1px solid #E2E5E7">
    <p style="margin:0;font-size:11px;color:#9AA1A5;line-height:1.6">Sent from the BYD virtual showroom because you asked for a summary. This is a demonstration build.</p>
  </td></tr>
</table>
</td></tr></table>
</body></html>""" % {
        "name": esc(name), "car": esc(car),
        "price": esc(lead.get("price") or PRICES.get(car, "")),
        "specs": spec_html, "covered": covered_html, "questions": questions_html,
        "dealer": esc(lead.get("dealer", "your local dealer")),
    }


def build_email_text(lead):
    car = lead.get("car", "BYD")
    name = (lead.get("name") or "").strip().split(" ")[0] or "there"
    lines = ["Hi %s," % name, "",
             "Here is your %s summary from the BYD virtual showroom." % car,
             "", "%s  %s" % (car, PRICES.get(car, "")), ""]
    for k, v in SPEC_ROWS.get(car, []):
        lines.append("  %-18s %s" % (k, v))
    covered = [STOP_LABELS.get(s, s) for s in lead.get("stopsVisited", [])]
    if covered:
        lines += ["", "We covered: " + ", ".join(covered)]
    qs = [q for q in lead.get("questions", []) if q][-8:]
    if qs:
        lines += ["", "You asked about:"] + ["  - " + q for q in qs]
    lines += ["", "Nearest dealer: " + str(lead.get("dealer", "")),
              "", "The full specification sheet is attached.", "",
              "Specifications from official BYD South Africa material.",
              "This is a demonstration build."]
    return "\n".join(lines)


def send_lead_email(lead):
    """Returns (ok, message). Called on a worker thread so the browser
    never waits on SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        return False, "Email not configured on the server"

    to = (lead.get("email") or "").strip()
    if not to:
        return False, "No recipient address"

    car = lead.get("car", "BYD")
    msg = EmailMessage()
    msg["Subject"] = "Your %s summary from BYD South Africa" % car
    msg["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    msg["To"] = to
    if MAIL_BCC:
        msg["Bcc"] = MAIL_BCC
    msg.set_content(build_email_text(lead))
    msg.add_alternative(build_email_html(lead), subtype="html")

    rel = BROCHURES.get(car)
    if rel:
        path = os.path.join(PUBLIC, rel)
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size < 12_000_000:      # stay well clear of the 25 MB ceiling
                with open(path, "rb") as fh:
                    msg.add_attachment(fh.read(), maintype="application",
                                       subtype="pdf",
                                       filename=os.path.basename(path))
            else:
                sys.stderr.write("  Brochure %.1f MB — too large to attach, "
                                 "sending without it\n" % (size / 1e6))

    try:
        if int(SMTP_PORT) == 465:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
        else:
            srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
            srv.starttls(context=ssl.create_default_context())
        with srv:
            srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(msg)
        sys.stderr.write("   Email sent to %s\n" % to)
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, ("SMTP login rejected. Gmail needs an App Password, "
                       "not your normal account password.")
    except smtplib.SMTPRecipientsRefused:
        return False, "The recipient address was refused"
    except Exception as e:
        return False, "SMTP error: %s" % e


class Upstream(Exception):
    """An API call failed. `status` is the HTTP code where there was one."""
    def __init__(self, message, status=None, detail=""):
        super().__init__(message)
        self.status = status
        self.detail = detail


def post_json(url, payload, headers, timeout=45):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise Upstream("HTTP %d" % e.code, e.code, raw[:400])
    except Exception as e:
        raise Upstream("unreachable", None, str(e)[:200])


def get_json(url, headers, timeout=30):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise Upstream("HTTP %d" % e.code, e.code, raw[:400])
    except Exception as e:
        raise Upstream("unreachable", None, str(e)[:200])


def gemini_auth(style):
    if style == "bearer":
        return {"content-type": "application/json",
                "authorization": "Bearer " + GEMINI_KEY}
    if style == "query":
        return {"content-type": "application/json"}
    return {"content-type": "application/json", "x-goog-api-key": GEMINI_KEY}


def gemini_endpoint(path, style):
    url = "https://generativelanguage.googleapis.com/v1beta/" + path
    if style == "query":
        url += ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(GEMINI_KEY)
    return url


def list_gemini_models():
    """Ask Google which models this key can actually use. Model IDs change
    often and old ones get retired, so discovering beats hard-coding."""
    global _gemini_auth_style
    styles = [_gemini_auth_style] if _gemini_auth_style else ["x-goog-api-key", "bearer", "query"]
    last = None
    for style in styles:
        try:
            body = get_json(gemini_endpoint("models?pageSize=200", style), gemini_auth(style))
        except Upstream as e:
            last = e
            if e.status in (401, 403):
                continue
            raise
        _gemini_auth_style = style
        names = []
        for m in body.get("models", []):
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            names.append(m.get("name", "").replace("models/", ""))
        return names
    raise last or Upstream("could not list models", 401)


def score_model(name):
    """Prefer newest stable Flash: fast, cheap, and on the free tier."""
    n = name.lower()
    if any(bad in n for bad in ("embedding", "aqa", "image", "vision", "tts",
                                "audio", "veo", "imagen", "learnlm", "gemma")):
        return -1
    score = 0
    if "flash" in n: score += 100
    if "lite" in n: score -= 12          # usable, but weaker at structured output
    if "pro" in n: score += 20           # usually not free, so below flash
    if "preview" in n or "exp" in n: score -= 25
    # Favour the highest version number present.
    ver = re.search(r"(\d+)(?:\.(\d+))?", n)
    if ver:
        score += int(ver.group(1)) * 10 + int(ver.group(2) or 0)
    return score


def pick_gemini_model():
    names = list_gemini_models()
    ranked = sorted(((score_model(n), n) for n in names if score_model(n) > 0),
                    reverse=True)
    return (ranked[0][1] if ranked else None), names
    body = post_json(
        "https://api.anthropic.com/v1/messages",
        {"model": ANTHROPIC_MODEL, "max_tokens": max_tokens,
         "system": system, "messages": messages},
        {"content-type": "application/json", "x-api-key": ANTHROPIC_KEY,
         "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in body.get("content", [])
                   if b.get("type") == "text")


def call_gemini(system, messages, max_tokens=900):
    """Gemini's REST shape differs from Anthropic's: the system prompt is a
    separate field, roles are user/model rather than user/assistant, and text
    lives under candidates[].content.parts[].

    Google is migrating key formats — old 'AIza' standard keys and newer
    'AQ.Ab' auth keys — and sources disagree on which header the new ones
    want. So try each style in turn and remember whichever is accepted."""
    global _gemini_auth_style, GEMINI_MODEL

    contents = [
        {"role": "model" if m.get("role") == "assistant" else "user",
         "parts": [{"text": m.get("content", "")}]}
        for m in messages
    ]
    payload = {
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.8,
            "responseMimeType": "application/json",
            # Gemini 3 models reason before answering, and those tokens come
            # out of the same budget. Left on, the model can spend the whole
            # allowance thinking and return no text at all.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    base_path = "models/%s:generateContent" % GEMINI_MODEL
    styles = [_gemini_auth_style] if _gemini_auth_style else ["x-goog-api-key", "bearer", "query"]
    last = None

    for style in styles:
        try:
            body = post_json(gemini_endpoint(base_path, style), payload, gemini_auth(style))
        except Upstream as e:
            last = e
            if e.status in (401, 403):
                continue          # wrong auth style for this key — try next
            if e.status == 404 and GEMINI_MODEL not in _model_tried:
                # This model is retired or unavailable to the account. Ask
                # Google what we can use and work down the ranked list.
                _model_tried.add(GEMINI_MODEL)
                try:
                    _, available = pick_gemini_model()
                except Upstream:
                    available = []
                ranked = [n for n in
                          sorted(available, key=score_model, reverse=True)
                          if score_model(n) > 0 and n not in _model_tried]
                if ranked:
                    sys.stderr.write("  Model '%s' unavailable — trying '%s'\n"
                                     % (GEMINI_MODEL, ranked[0]))
                    GEMINI_MODEL = ranked[0]
                    return call_gemini(system, messages, max_tokens)
                e.detail = (e.detail or "") + " | none of these worked: " + \
                           (", ".join(sorted(_model_tried)) or "n/a")
            raise
        if _gemini_auth_style != style:
            _gemini_auth_style = style
            sys.stderr.write("  Gemini auth style accepted: %s\n" % style)

        cands = body.get("candidates") or []
        if not cands:
            blocked = (body.get("promptFeedback") or {}).get("blockReason")
            raise Upstream("no candidates" + (" (%s)" % blocked if blocked else ""),
                           None, json.dumps(body)[:300])

        cand = cands[0]
        parts = (cand.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts)
        if not text.strip():
            # Empty answer. Usually the reasoning budget ate the allowance,
            # or a safety filter stripped it. Say which, rather than failing
            # silently into the scripted fallback forever.
            reason = cand.get("finishReason", "unknown")
            usage = body.get("usageMetadata", {})
            sys.stderr.write("  Gemini returned no text. finishReason=%s usage=%s\n"
                             % (reason, usage))
            if reason == "MAX_TOKENS":
                raise Upstream("model hit the token limit before answering",
                               None, "finishReason=MAX_TOKENS usage=%s" % usage)
            if reason in ("SAFETY", "RECITATION", "PROHIBITED_CONTENT"):
                raise Upstream("the model declined to answer (%s)" % reason,
                               None, str(usage))
            raise Upstream("empty response (finishReason=%s)" % reason, None, str(usage))
        return text

    raise last or Upstream("all Gemini auth styles rejected", 401)


def generate(system, messages, max_tokens=900):
    p = active_provider()
    if p == "anthropic":
        return call_anthropic(system, messages, max_tokens)
    if p == "gemini":
        return call_gemini(system, messages, max_tokens)
    raise Upstream("no_key")


def advice_for(provider, err):
    """Turn an upstream failure into something a human can act on."""
    blob = (err.detail or "").lower()
    msg = str(getattr(err, "args", [""])[0] if err.args else "").lower()

    if "max_tokens" in blob or "token limit" in msg:
        return ("The model used its whole output allowance before answering. "
                "Thinking is already disabled; try raising max_tokens in "
                "call_gemini, or switch GEMINI_MODEL to a non-reasoning model.")
    if "declined" in msg or "safety" in blob:
        return "The model declined to answer that one. Rephrasing usually fixes it."
    if "empty response" in msg:
        return "The model returned nothing. Retry, or try a different GEMINI_MODEL."
    if "allowlist" in blob or "egress" in blob or "proxy" in blob:
        return ("The request was blocked before it reached the API — a network "
                "policy, VPN or firewall is in the way, not the key itself.")
    if err.status in (401, 403):
        if provider == "gemini":
            return ("Google rejected the key (%d) on every auth style tried. "
                    "If it is an 'AQ.' auth key, check it is not expired and that "
                    "the Gemini API is enabled on its Cloud project. If it is an "
                    "older 'AIza' key, it needs a restriction applied — unrestricted "
                    "standard keys stopped working in June 2026." % err.status)
        return "The key was rejected (%d). Check for typos, stray quotes or spaces." % err.status
    if err.status == 404:
        if provider == "gemini":
            listed = ""
            try:
                _, names = pick_gemini_model()
                usable = [n for n in names if score_model(n) > 0][:6]
                if usable:
                    listed = " Models your key can use: " + ", ".join(usable) + "."
            except Exception:
                pass
            return ("Model '%s' is not available to this account.%s "
                    "Set GEMINI_MODEL in your env file to one of those."
                    % (GEMINI_MODEL, listed))
        return ("Model '%s' was not found. Try ANTHROPIC_MODEL=claude-sonnet-4-6."
                % ANTHROPIC_MODEL)
    if err.status == 429:
        return "Rate limited or out of quota. Wait a moment and try again."
    if err.status == 400:
        return "The request was rejected as malformed — most likely a bad model name."
    if err.status:
        return "Unexpected HTTP %d from the API." % err.status
    return ("Could not reach the API. Check your internet connection, VPN, "
            "or corporate firewall.")


def append(filename, record):
    os.makedirs(DATA, exist_ok=True)
    record["at"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(os.path.join(DATA, filename), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_all(filename):
    path = os.path.join(DATA, filename)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=PUBLIC, **kwargs)

    def log_message(self, fmt, *args):
        if "/api/" in str(args):
            sys.stderr.write("  %s\n" % (fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        if self.path == "/api/status":
            p = active_provider()
            smtp_ok = bool(SMTP_USER and SMTP_PASS and "@" in SMTP_USER)
            note = ""
            if not (SMTP_USER and SMTP_PASS):
                note = "SMTP_USER / SMTP_PASS are empty in server.py"
            elif "@" not in SMTP_USER:
                note = ("SMTP_USER is '%s', which is not an email address"
                        % SMTP_USER)
            elif len(SMTP_PASS) != 16:
                note = ("App password is %d characters, expected 16"
                        % len(SMTP_PASS))
            return self._json(200, {
                "hasKey": bool(p), "provider": p or "none", "model": active_model(),
                "smtpConfigured": smtp_ok, "smtpUser": SMTP_USER if smtp_ok else "",
                "smtpNote": note})

        if self.path == "/api/diag":
            p = active_provider()
            info = {
                "provider": p or "none",
                "model": active_model(),
                "anthropicKey": preview(ANTHROPIC_KEY),
                "geminiKey": preview(GEMINI_KEY),
            }
            if not p:
                info["result"] = "no_key"
                info["advice"] = ("No API key found. Set ANTHROPIC_API_KEY or GEMINI_API_KEY "
                                  "in the terminal that starts this server, or put it in a "
                                  ".env file next to server.py.")
                return self._json(200, info)

            try:
                text = generate("Reply with the single word OK.",
                                [{"role": "user", "content": "Say OK"}], max_tokens=8)
                info["result"] = "ok"
                info["reply"] = text[:80]
                info["authStyle"] = _gemini_auth_style or "n/a"
                info["advice"] = "Connected. %s is working with %s." % (p.title(), active_model())
            except Upstream as e:
                info["result"] = "http_%s" % (e.status or "unreachable")
                info["detail"] = e.detail
                info["advice"] = advice_for(p, e)
            sys.stderr.write("  Diagnostic [%s]: %s — %s\n"
                             % (p, info["result"], info["advice"]))
            return self._json(200, info)

        if self.path == "/api/report":
            leads = read_all("leads.jsonl")
            sessions = read_all("sessions.jsonl")
            stops, questions, drops = {}, [], {}
            for s in sessions:
                for name in s.get("stopsVisited", []):
                    stops[name] = stops.get(name, 0) + 1
                questions.extend(s.get("questions", []))
                last = s.get("lastStop") or "not started"
                drops[last] = drops.get(last, 0) + 1
            return self._json(200, {
                "sessions": len(sessions),
                "leads": len(leads),
                "conversionPercent": round(len(leads) / len(sessions) * 100, 1) if sessions else 0,
                "stopVisits": stops,
                "exitPoints": drops,
                "recentQuestions": questions[-120:],
            })

        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/lead":
            data = self._body()
            if data is None:
                return self._json(400, {"error": "bad_json"})
            append("leads.jsonl", data)
            sys.stderr.write("   Lead: %s / %s\n" % (
                data.get("name", "unnamed"), data.get("email", "no email")))

            if not (SMTP_USER and SMTP_PASS):
                return self._json(200, {"ok": True, "emailed": False,
                                        "emailNote": "Saved. Email is not configured "
                                                     "on the server, so nothing was sent."})

            # Send on a worker thread — SMTP can take several seconds and the
            # person should not be staring at a spinner for it.
            threading.Thread(target=lambda: send_lead_email(data),
                             daemon=True).start()
            return self._json(200, {"ok": True, "emailed": True})

        if self.path == "/api/mailtest":
            data = self._body() or {}
            to = (data.get("email") or SMTP_USER or "").strip()
            if not to:
                return self._json(200, {"ok": False,
                                        "detail": "No address to send to."})
            ok, detail = send_lead_email({
                "email": to, "name": "Test", "car": data.get("car", "BYD Seal"),
                "dealer": "Sandton",
                "stopsVisited": ["stance", "charge", "v2l"],
                "questions": ["What does it cost to charge at home?"],
            })
            sys.stderr.write("  Mail test -> %s: %s\n" % (to, detail))
            return self._json(200, {"ok": ok, "detail": detail, "to": to})

        if self.path == "/api/session":
            data = self._body()
            if data is None:
                return self._json(400, {"error": "bad_json"})
            append("sessions.jsonl", data)
            return self._json(200, {"ok": True})

        if self.path != "/api/chat":
            return self._json(404, {"error": "unknown_endpoint"})

        p = active_provider()
        if not p:
            return self._json(503, {"error": "no_key"})

        incoming = self._body()
        if incoming is None:
            return self._json(400, {"error": "bad_json"})

        try:
            text = generate(incoming.get("system", ""), incoming.get("messages", []))
            return self._json(200, {"text": text, "provider": p})
        except Upstream as e:
            sys.stderr.write("  Chat failed [%s]: %s %s\n" % (p, e, e.detail[:200]))
            return self._json(502, {"error": "upstream", "status": e.status,
                                    "advice": advice_for(p, e)})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


BUILD = "v11 \u2014 finds index.html either layout"


def preflight():
    print("\n  BYD Showroom server  [%s]" % BUILD)
    missing = [
        name for name in ("2024_byd_seal.glb", "2024_byd_sealion_7.glb")
        if not os.path.exists(os.path.join(PUBLIC, "models", name))
    ]
    if missing:
        print("  Vehicle models not found in ./public/models/:")
        for name in missing:
            print("     - " + name)
        print("   See docs/ASSETS.md for where to get them.\n")


    p = active_provider()
    if p == "gemini":
        global GEMINI_MODEL
        print("  Provider: GEMINI   key %s" % preview(GEMINI_KEY))
        print("  Asking Google which models this key can use…")
        try:
            picked, names = pick_gemini_model()
            usable = [n for n in names if score_model(n) > 0]
            if GEMINI_MODEL and GEMINI_MODEL in names:
                print("  Model:    %s (you pinned this; confirmed available)" % GEMINI_MODEL)
            elif picked:
                if GEMINI_MODEL:
                    print("  Model:    '%s' is NOT available — using '%s' instead"
                          % (GEMINI_MODEL, picked))
                else:
                    print("  Model:    %s (chosen automatically)" % picked)
                GEMINI_MODEL = picked
            else:
                print("  Model:    no usable text model found on this key.")
            if usable:
                print("  Also available: %s" % ", ".join(usable[:6]))
        except Upstream as e:
            print("  Model:    could not verify (%s)" % e)
            print("            %s" % advice_for("gemini", e))
            if not GEMINI_MODEL:
                GEMINI_MODEL = "gemini-3-flash"
                print("            Falling back to '%s'; will auto-correct on first"
                      % GEMINI_MODEL)
                print("            request if that one is unavailable.")
    elif p == "anthropic":
        print("  Provider: ANTHROPIC   key %s" % preview(ANTHROPIC_KEY))
        print("  Model:    %s" % ANTHROPIC_MODEL)
        if not ANTHROPIC_KEY.startswith("sk-ant-"):
            print("            WARNING: that does not look like an Anthropic key.")
    else:
        print("  Provider: NONE — Naledi will only give scripted answers.")
        print("\n  Files sitting next to server.py right now:")
        try:
            for name in sorted(os.listdir(ROOT)):
                if not os.path.isdir(os.path.join(ROOT, name)):
                    print("     " + name)
        except OSError:
            pass
        print("\n  Expected a file called '.env' containing:")
        print("     GEMINI_API_KEY=your-key-here")
        print("  Windows often saves it as '.env.txt' — that is fine, this")
        print("  server accepts that name too. If you see no env file above,")
        print("  it is in a different folder from the one you are running.")
        print("\n  Or set it in THIS terminal before starting:")
        print('     $env:GEMINI_API_KEY="your-key-here"')

    print("\n  Serving   %s" % (
        "public/" if PUBLIC.endswith("public") else "repository root"))
    if not os.path.exists(os.path.join(PUBLIC, "index.html")):
        print("            WARNING: no index.html found. Files here:")
        try:
            for n in sorted(os.listdir(PUBLIC))[:12]:
                print("              " + n)
        except OSError:
            pass
    print("  Listening on %s:%d" % (HOST, PORT))
    print("  Showroom    http://localhost:%d" % PORT)
    print("  Mic check   http://localhost:%d/mic-test.html" % PORT)
    print("  Diagnose    http://localhost:%d/api/diag" % PORT)
    print("  Email test  http://localhost:%d/mail-test.html" % PORT)
    print("  Lead data   http://localhost:%d/api/report" % PORT)
    if SMTP_USER and SMTP_PASS:
        if "@" not in SMTP_USER:
            print("  Email:      MISCONFIGURED")
            print("              SMTP_USER is '%s', which is not an email address."
                  % SMTP_USER)
            print("              It must be the full Gmail address you created the")
            print("              app password under, e.g. yourname@gmail.com")
            print("              (the name you gave the app password is not used).")
        elif len(SMTP_PASS) != 16:
            print("  Email:      configured as %s" % SMTP_USER)
            print("              WARNING: app password is %d characters, expected 16."
                  % len(SMTP_PASS))
        else:
            print("  Email:      configured as %s" % SMTP_USER)
    else:
        print("  Email:      NOT configured — leads save to disk but nothing sends.")
        print("              Set SMTP_USER and SMTP_PASS near the top of server.py.")
    print("\n  Use Chrome or Edge for microphone support. Ctrl+C to stop.\n")


if __name__ == "__main__":
    preflight()
    # Bind to every interface. Hosting platforms (Render, Railway, Fly)
    # route traffic to the container's external address and will not see
    # a server listening only on localhost. This still answers on
    # http://localhost:PORT during development, so nothing changes there.
    with Server((HOST, PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Showroom closed.")
