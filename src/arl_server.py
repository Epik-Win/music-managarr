#!/usr/bin/env python3
"""
ARL refresh web page
====================
Lets the admin renew the Deezer ARL (the login cookie deemix downloads
with) from a browser. Put it behind a reverse proxy at ARL_PUBLIC_URL to
reach it from outside the LAN.

Port lifecycle
--------------
The server only binds to PORT when the ARL is stale (arl_stale.flag exists).
When the ARL is NOT stale the port is closed/unbound so the web interface is
completely inaccessible — no connections are accepted.

Flow:
  1. Main loop polls arl_stale.flag every POLL_INTERVAL seconds.
  2. Flag appears  → bind to PORT, start accepting HTTP connections.
  3. ARL written   → close server socket, port released, loop continues polling.
  4. Flag re-appears → bind again.

Authentication flow
-------------------
1. User clicks "Continue with Deezer" -> redirected to Deezer OAuth2 page.
2. User logs in on Deezer's own servers (no server-side credential passing).
3. Deezer redirects to /callback?code=AUTH_CODE.
4. Server exchanges code for access_token.
5. Server loads deezer.com with the token cookie, which triggers Deezer to
   issue a full session including the `arl` cookie.
6. ARL written to ~/.config/deemix/.arl.
7. downloader_manager.wait_for_arl_refresh() detects the change within 60s.
8. Server port is closed immediately after the ARL is written.

Fallback: manual ARL paste tab (no OAuth needed).

One-time setup
--------------
1. Register a free app at https://developers.deezer.com/myapps
     Application domain : the domain of ARL_PUBLIC_URL
     Redirect URL       : <ARL_PUBLIC_URL>/callback
2. Set in docker-compose:
     DEEZER_APP_ID=<your app id>
     DEEZER_APP_SECRET=<your app secret>
     ARL_PUBLIC_URL=https://arl.example.com          # where the page is reachable
     DEEZER_REDIRECT_URI=https://arl.example.com/callback   # default: ARL_PUBLIC_URL + /callback
     PORT=8888
     ARL_SERVER_SECRET=<a passphrase>
"""

import os, time, secrets, logging, threading
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode, parse_qs
from wsgiref.simple_server import make_server, WSGIRequestHandler

import requests
from flask import (Flask, request, jsonify, redirect,
                   url_for, session, render_template_string)

# ---------------------------------------------------------------------------
PORT            = int(os.environ.get("PORT", 8888))
DATA_DIR        = os.environ.get("DATA_DIR", "/app/data")
SECRET          = os.environ.get("ARL_SERVER_SECRET", "musicai")
APP_ID          = os.environ.get("DEEZER_APP_ID", "")
APP_SECRET      = os.environ.get("DEEZER_APP_SECRET", "")
PUBLIC_URL      = os.environ.get("ARL_PUBLIC_URL", "").strip().rstrip("/")
REDIRECT_URI    = os.environ.get("DEEZER_REDIRECT_URI", "").strip() or (
    f"{PUBLIC_URL}/callback" if PUBLIC_URL else "")
ARL_STALE_FLAG    = os.path.join(DATA_DIR, "arl_stale.flag")
ARL_REFRESH_NONCE = os.path.join(DATA_DIR, "arl_refresh.nonce")
ARL_CONFIG_FILE   = Path.home() / ".config" / "deemix" / ".arl"
PROJECT_ENV_FILE  = Path(os.environ.get("PROJECT_ENV_FILE", "/app/data/project.env"))

# How often (seconds) the main loop checks whether the stale flag has appeared
POLL_INTERVAL = 15

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [ARL-SERVER] %(levelname)s %(message)s")
logger = logging.getLogger("arl_server")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", secrets.token_hex(24))

# ---------------------------------------------------------------------------
# Global server state — controlled by the lifecycle loop
# ---------------------------------------------------------------------------

_server_lock   = threading.Lock()
_http_server   = None          # werkzeug WSGIServer instance when port is open
_server_thread = None          # thread running _http_server.serve_forever()
_shutdown_flag = threading.Event()   # set inside a route to close the port

# ---------------------------------------------------------------------------
# ARL helpers
# ---------------------------------------------------------------------------

def is_stale() -> bool:
    return os.path.exists(ARL_STALE_FLAG)

def current_arl() -> str:
    try:
        if ARL_CONFIG_FILE.exists():
            return ARL_CONFIG_FILE.read_text().strip()
    except Exception:
        pass
    return os.environ.get("DEEZER_ARL", "")


def _persist_arl_to_project_env(arl: str) -> None:
    """Keep the project .env file synchronized with the latest ARL."""
    env_path = Path(os.environ.get("PROJECT_ENV_FILE", str(PROJECT_ENV_FILE)))
    env_path.parent.mkdir(parents=True, exist_ok=True)
    arl_value = arl.strip()

    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []

    replaced = False
    updated_lines = []
    for line in lines:
        if line.strip().startswith("DEEZER_ARL="):
            updated_lines.append(f"DEEZER_ARL={arl_value}")
            replaced = True
        else:
            updated_lines.append(line)

    if not replaced:
        updated_lines.append(f"DEEZER_ARL={arl_value}")

    env_path.write_text("\n".join(updated_lines) + "\n")
    os.environ["DEEZER_ARL"] = arl_value
    logger.info(f"ARL persisted to project env file: {env_path}")


def write_arl(arl: str) -> bool:
    """Write the ARL to disk, keep the env file in sync, clear stale state, and signal port shutdown."""
    arl_value = arl.strip()
    if not arl_value:
        return False

    try:
        ARL_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        ARL_CONFIG_FILE.write_text(arl_value)
        logger.info(f"ARL written to {ARL_CONFIG_FILE} (len={len(arl_value)})")
    except Exception as e:
        logger.error(f"Could not write ARL: {e}")
        return False

    try:
        _persist_arl_to_project_env(arl_value)
    except Exception as e:
        logger.warning(f"Could not persist ARL to project .env file: {e}")

    # Write a nonce so wait_for_arl_refresh() detects the change
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ARL_REFRESH_NONCE, "w") as _nf:
            _nf.write(str(time.time()))
        logger.info(f"Refresh nonce written: {ARL_REFRESH_NONCE}")
    except Exception as e:
        logger.warning(f"Could not write refresh nonce: {e}")

    # Clear the stale flag
    try:
        if os.path.exists(ARL_STALE_FLAG):
            os.remove(ARL_STALE_FLAG)
            logger.info(f"Stale flag cleared: {ARL_STALE_FLAG}")
    except Exception as e:
        logger.warning(f"Could not clear stale flag: {e}")

    # Signal the lifecycle loop to close the port
    _shutdown_flag.set()
    logger.info("Port shutdown flag set — port will close after this request completes")
    return True


# ---------------------------------------------------------------------------
# Port lifecycle helpers
# ---------------------------------------------------------------------------

class _SilentHandler(WSGIRequestHandler):
    """Suppress the per-request access log lines from wsgiref."""
    def log_message(self, fmt, *args):  # noqa: D401
        pass


def _open_port() -> bool:
    """Bind to PORT and start serving in a background thread.
    Returns True if the port was successfully opened.
    """
    global _http_server, _server_thread
    with _server_lock:
        if _http_server is not None:
            return True  # already open
        try:
            _shutdown_flag.clear()
            srv = make_server("0.0.0.0", PORT, app,
                              handler_class=_SilentHandler)
            srv.timeout = 1          # so serve_forever() polls shutdown flag
            _http_server = srv
            _server_thread = threading.Thread(
                target=srv.serve_forever,
                daemon=True,
                name="arl-http",
            )
            _server_thread.start()
            logger.info(f"🔓 ARL server port {PORT} OPENED — waiting for ARL refresh")
            return True
        except OSError as exc:
            logger.error(f"Could not bind to port {PORT}: {exc}")
            _http_server = None
            return False


def _close_port() -> None:
    """Shut down the HTTP server and release the port."""
    global _http_server, _server_thread
    with _server_lock:
        srv = _http_server
        thr = _server_thread
        _http_server   = None
        _server_thread = None

    if srv is not None:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception as exc:
            logger.debug(f"Error closing server: {exc}")
        logger.info(f"🔒 ARL server port {PORT} CLOSED")

    if thr is not None:
        thr.join(timeout=5)


def _port_is_open() -> bool:
    return _http_server is not None


# ---------------------------------------------------------------------------
# Deezer OAuth2 helpers
# ---------------------------------------------------------------------------

DEEZER_AUTH_URL  = "https://connect.deezer.com/oauth/auth.php"
DEEZER_TOKEN_URL = "https://connect.deezer.com/oauth/access_token.php"
DEEZER_PERMS     = "basic_access,email,offline_access,manage_library,listening_history"

def oauth_available() -> bool:
    return bool(APP_ID and APP_SECRET)

def build_auth_url(state: str) -> str:
    params = {"app_id": APP_ID, "redirect_uri": REDIRECT_URI,
              "perms": DEEZER_PERMS, "state": state}
    return f"{DEEZER_AUTH_URL}?{urlencode(params)}"

def exchange_code(code: str):
    """Returns (access_token, error)."""
    try:
        r = requests.get(DEEZER_TOKEN_URL, params={
            "app_id": APP_ID, "secret": APP_SECRET,
            "code": code, "output": "json"}, timeout=15)
        r.raise_for_status()
        text = r.text.strip()
        if text.startswith("{"):
            token = r.json().get("access_token")
        else:
            token = parse_qs(text).get("access_token", [None])[0]
        return (token, None) if token else (None, f"Unexpected response: {text[:200]}")
    except Exception as e:
        return None, f"Token exchange error: {e}"

def arl_from_token(access_token: str):
    """
    Try three methods to coerce an OAuth access_token into an ARL cookie.
    Returns (arl, error).
    """
    S = requests.Session()
    S.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Method 1: set access_token as cookie, visit deezer.com -> they set arl
    try:
        S.cookies.set("access_token", access_token, domain=".deezer.com")
        S.get("https://www.deezer.com/en/", timeout=15, allow_redirects=True)
        arl = S.cookies.get("arl")
        if arl and len(arl) > 100:
            logger.info(f"ARL via session cookie (len={len(arl)})")
            return arl, None
    except Exception as e:
        logger.warning(f"Method 1 failed: {e}")

    # Method 2: internal gateway API
    try:
        gw = S.get("https://www.deezer.com/ajax/gw-light.php", params={
            "method": "deezer.getUserData", "input": "3",
            "api_version": "1.0", "api_token": "null", "cid": "0"},
            headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        gw_data = gw.json()
        sid = (gw_data.get("results", {}).get("SESSION_ID") or
               gw_data.get("results", {}).get("checkForm"))
        arl = S.cookies.get("arl")
        if arl and len(arl) > 100:
            logger.info(f"ARL via gateway cookie (len={len(arl)})")
            return arl, None
        if sid and len(sid) > 100:
            logger.info(f"ARL via gateway SID (len={len(sid)})")
            return sid, None
    except Exception as e:
        logger.warning(f"Method 2 failed: {e}")

    # Method 3: API user/me then recheck cookies
    try:
        S.get("https://api.deezer.com/user/me",
              params={"access_token": access_token}, timeout=10)
        arl = S.cookies.get("arl")
        if arl and len(arl) > 100:
            logger.info(f"ARL via /user/me cookie (len={len(arl)})")
            return arl, None
    except Exception as e:
        logger.warning(f"Method 3 failed: {e}")

    return None, (
        "OAuth succeeded but Deezer did not return the ARL cookie automatically. "
        "You are now logged in — switch to the Manual ARL tab and paste the `arl` "
        "cookie value from DevTools (Application -> Cookies -> deezer.com -> arl)."
    )

# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>ARL Refresh — Music AI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#080b12;--surf:#0f1320;--surf2:#161c2e;--bdr:#232840;
  --acc:#6c63ff;--txt:#dde3f5;--mut:#5a6384;
  --ok:#3ecf8e;--warn:#f7c94b;--err:#ff6584;
  --font:'Syne',sans-serif;--mono:'DM Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--font);background:var(--bg);color:var(--txt);
  min-height:100vh;display:flex;flex-direction:column;
  align-items:center;justify-content:center;padding:2rem 1rem;
  background-image:radial-gradient(ellipse 70% 50% at 15% 5%,rgba(108,99,255,.13) 0%,transparent 55%),
    radial-gradient(ellipse 50% 40% at 85% 95%,rgba(255,101,132,.08) 0%,transparent 55%);}
.wrap{width:100%;max-width:500px;animation:up .4s cubic-bezier(.22,1,.36,1) both}
@keyframes up{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.hdr{text-align:center;margin-bottom:2rem}
.badge{display:inline-flex;align-items:center;gap:.4rem;
  background:rgba(108,99,255,.12);border:1px solid rgba(108,99,255,.25);
  border-radius:100px;padding:.28rem .75rem;margin-bottom:1rem}
.badge-dot{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 8px var(--acc)}
.badge-txt{font-size:.68rem;letter-spacing:.15em;text-transform:uppercase;color:var(--acc)}
h1{font-size:1.85rem;font-weight:800;
  background:linear-gradient(125deg,var(--txt) 30%,var(--mut));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.sub{font-size:.8rem;color:var(--mut);font-family:var(--mono);margin-top:.45rem;line-height:1.5}
.status{display:flex;align-items:center;gap:.65rem;padding:.65rem 1rem;
  background:var(--surf);border:1px solid var(--bdr);border-radius:8px;
  margin-bottom:1.3rem;font-size:.77rem;font-family:var(--mono)}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.dot.stale{background:var(--err);box-shadow:0 0 8px var(--err);animation:blink 1.4s ease infinite}
.dot.ok{background:var(--ok);box-shadow:0 0 8px var(--ok)}
.dot.unk{background:var(--mut)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.s-lbl{flex:1}.s-time{color:var(--mut);font-size:.69rem}
.alert{padding:.72rem .95rem;border-radius:8px;font-size:.79rem;
  font-family:var(--mono);margin-bottom:1rem;line-height:1.55;display:flex;gap:.55rem}
.ae{background:rgba(255,101,132,.09);border:1px solid rgba(255,101,132,.28);color:var(--err)}
.ao{background:rgba(62,207,142,.09);border:1px solid rgba(62,207,142,.28);color:var(--ok)}
.ai{background:rgba(247,201,75,.08);border:1px solid rgba(247,201,75,.22);color:var(--warn)}
.card{background:var(--surf);border:1px solid var(--bdr);border-radius:14px;
  padding:1.55rem;margin-bottom:.85rem}
.ctitle{font-size:.67rem;font-weight:700;letter-spacing:.15em;text-transform:uppercase;
  color:var(--acc);margin-bottom:1.15rem;display:flex;align-items:center;gap:.45rem}
.ctitle::after{content:'';flex:1;height:1px;background:var(--bdr)}
.tabs{display:flex;gap:.35rem;background:var(--surf2);border-radius:8px;padding:3px;margin-bottom:1.2rem}
.tb{flex:1;padding:.42rem;background:transparent;border:none;border-radius:6px;
  color:var(--mut);font-family:var(--font);font-size:.77rem;font-weight:700;cursor:pointer;
  transition:background .18s,color .18s}
.tb.on{background:var(--surf);color:var(--txt);box-shadow:0 1px 5px rgba(0,0,0,.4)}
.tc{display:none}.tc.on{display:block}
label{display:block;font-size:.7rem;font-weight:700;letter-spacing:.06em;
  color:var(--mut);text-transform:uppercase;margin-bottom:.32rem}
input,textarea{width:100%;padding:.62rem .82rem;background:var(--surf2);
  border:1px solid var(--bdr);border-radius:7px;color:var(--txt);
  font-family:var(--mono);font-size:.87rem;outline:none;
  transition:border-color .17s,box-shadow .17s;margin-bottom:.85rem}
input:focus,textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(108,99,255,.14)}
textarea{resize:vertical;min-height:76px;font-size:.75rem}
.hint{font-size:.69rem;color:var(--mut);font-family:var(--mono);margin-top:-.45rem;margin-bottom:.85rem}
.btn{width:100%;padding:.78rem 1.35rem;border:none;border-radius:8px;
  font-family:var(--font);font-size:.87rem;font-weight:700;
  cursor:pointer;transition:transform .11s,opacity .11s,box-shadow .11s;position:relative}
.btn:active{transform:scale(.98)}
.btn-p{background:var(--acc);color:#fff;box-shadow:0 4px 22px rgba(108,99,255,.35)}
.btn-p:hover{opacity:.9;box-shadow:0 4px 30px rgba(108,99,255,.5)}
.btn-s{background:var(--surf2);color:var(--txt);border:1px solid var(--bdr);margin-top:.4rem}
.btn-s:hover{border-color:var(--mut)}
.spin{display:none;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:sp .5s linear infinite;
  position:absolute;right:.85rem;top:50%;transform:translateY(-50%)}
@keyframes sp{to{transform:translateY(-50%) rotate(360deg)}}
.oauth-btn{display:flex;align-items:center;justify-content:center;gap:.65rem;
  width:100%;padding:.88rem 1.35rem;
  background:linear-gradient(135deg,#6c63ff 0%,#8b82ff 100%);
  border:none;border-radius:9px;color:#fff;font-family:var(--font);
  font-size:.9rem;font-weight:800;cursor:pointer;text-decoration:none;
  box-shadow:0 6px 28px rgba(108,99,255,.4);transition:transform .12s,box-shadow .12s}
.oauth-btn:hover{transform:translateY(-1px);box-shadow:0 8px 36px rgba(108,99,255,.55)}
.dz-icon{width:22px;height:22px;fill:#fff;flex-shrink:0}
.oauth-desc{font-size:.8rem;color:var(--mut);font-family:var(--mono);
  line-height:1.6;margin-bottom:1.1rem}
.setup-box{background:rgba(247,201,75,.06);border:1px solid rgba(247,201,75,.2);
  border-radius:10px;padding:1.1rem;font-size:.78rem;font-family:var(--mono);
  line-height:1.75;color:var(--warn)}
.setup-box code{background:rgba(247,201,75,.12);border-radius:4px;padding:.1em .32em}
.setup-box a{color:var(--acc);text-decoration:none}
.arl-pre{font-family:var(--mono);font-size:.7rem;color:var(--mut);word-break:break-all;
  background:var(--surf2);border:1px solid var(--bdr);border-radius:6px;
  padding:.52rem .72rem;margin-top:.45rem}
.foot{text-align:center;font-size:.69rem;font-family:var(--mono);
  color:var(--mut);margin-top:1.2rem;line-height:1.7}
.foot a{color:var(--acc);text-decoration:none}
</style>
</head>
<body>
<div class="wrap">
<div class="hdr">
  <div class="badge"><span class="badge-dot"></span><span class="badge-txt">Music AI</span></div>
  <h1>Refresh ARL Token</h1>
  <p class="sub">Deezer authentication required to resume downloads</p>
</div>
<div class="status">
  {% if stale %}<div class="dot stale"></div><span class="s-lbl">Downloads paused — ARL token expired</span>
  {% elif arl_ok %}<div class="dot ok"></div><span class="s-lbl">ARL active — downloads running</span>
  {% else %}<div class="dot unk"></div><span class="s-lbl">Awaiting authentication</span>{% endif %}
  <span class="s-time">{{ now }}</span>
</div>
{% if err %}<div class="alert ae">⚠ {{ err }}</div>{% endif %}
{% if ok  %}<div class="alert ao">✓ {{ ok  }}</div>{% endif %}
{% if inf %}<div class="alert ai">ℹ {{ inf }}</div>{% endif %}
{% if not ok %}
<div class="card">
  <div class="ctitle">Authenticate</div>
  <div class="tabs">
    <button class="tb on" onclick="tab('oauth',this)">Deezer OAuth</button>
    <button class="tb" onclick="tab('manual',this)">Manual ARL</button>
  </div>
  <div class="tc on" id="tc-oauth">
    {% if oauth_ready %}
    <p class="oauth-desc">
      Click below — you will be taken to <strong style="color:var(--txt)">deezer.com</strong>
      to log in. Your credentials never touch this server.
      The ARL is captured automatically on return.
    </p>
    <a class="oauth-btn" href="/oauth/start" onclick="this.querySelector('.spin').style.display='block'">
      <svg class="dz-icon" viewBox="0 0 24 24"><path d="M18.81 11.28h-2.35v2.36h2.35zm0-4.71h-2.35v2.36h2.35zm0 9.43h-2.35V18.36h2.35zM5.19 16h2.35v-2.36H5.19zm4.71 0h2.35v-2.36H9.9zm4.71 0h-2.36v2.36h2.36zm-4.71-4.72h2.35v-2.36H9.9zm-4.71 0h2.35v-2.36H5.19zm9.42 0h-2.36v2.36h2.36zM5.19 6.57h2.35V4.21H5.19zM9.9 6.57h2.35V4.21H9.9zm4.71 0h-2.36v2.36h2.36zM9.9 11.28h2.35V8.92H9.9z"/></svg>
      Continue with Deezer
      <div class="spin"></div>
    </a>
    {% else %}
    <div class="setup-box">
      <strong>OAuth not configured</strong> — set up a free Deezer app first:<br><br>
      1. Go to <a href="https://developers.deezer.com/myapps" target="_blank">developers.deezer.com/myapps</a><br>
      2. Create an app — set redirect URL to:<br>
      &nbsp;&nbsp;&nbsp;<code>{{ redirect_uri or "<ARL_PUBLIC_URL>/callback" }}</code><br>
      3. Add to docker-compose environment:<br>
      &nbsp;&nbsp;&nbsp;<code>DEEZER_APP_ID=your_app_id</code><br>
      &nbsp;&nbsp;&nbsp;<code>DEEZER_APP_SECRET=your_app_secret</code><br><br>
      Until then, use the <strong>Manual ARL</strong> tab.
    </div>
    {% endif %}
  </div>
  <div class="tc" id="tc-manual">
    <div class="alert ai" style="margin-bottom:1rem">ℹ
      <span>Log in at <strong>deezer.com</strong> → F12 → Application →
      Cookies → deezer.com → copy value of <strong>arl</strong></span>
    </div>
    <form method="POST" action="/submit-arl" onsubmit="go('btn-m')">
      <label for="av">ARL Token</label>
      <textarea id="av" name="arl" placeholder="Paste ~192-char hex ARL token…" required></textarea>
      <label for="sm">Server Passphrase</label>
      <input type="password" id="sm" name="secret" placeholder="ARL_SERVER_SECRET" required>
      <div class="hint">Set via <code>ARL_SERVER_SECRET</code> env var.</div>
      <button class="btn btn-p" type="submit" id="btn-m">Submit ARL<div class="spin"></div></button>
    </form>
  </div>
</div>
{% else %}
<div class="card">
  <div class="ctitle">Token Saved</div>
  <div class="alert ao">
    ✓ ARL written to <code>~/.config/deemix/.arl</code><br>
    All paused downloads resume automatically within 60 seconds.<br>
    This page will become inaccessible momentarily.
  </div>
  {% if arl_preview %}<div class="arl-pre">{{ arl_preview }}… <span style="opacity:.4">(first 32 chars)</span></div>{% endif %}
  <a href="/" class="btn btn-s" style="display:block;text-align:center;text-decoration:none;margin-top:.9rem">Back</a>
</div>
{% endif %}
<div class="foot">
  {% if public_url %}{{ public_url }} &middot; {% endif %}Music AI &middot; <a href="/status">JSON status</a>
</div>
</div>
<script>
function tab(n,b){
  document.querySelectorAll('.tb').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.tc').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');
  document.getElementById('tc-'+n).classList.add('on');
}
function go(id){
  const b=document.getElementById(id);
  if(!b)return;b.disabled=true;
  const s=b.querySelector('.spin');if(s)s.style.display='block';
}
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    arl = current_arl()
    err = session.pop("err", None)
    ok  = session.pop("ok",  None)
    inf = session.pop("inf", None)
    preview = session.pop("arl_preview", None)
    return render_template_string(HTML,
        stale=is_stale(), arl_ok=bool(arl),
        now=datetime.now().strftime("%H:%M:%S"),
        oauth_ready=oauth_available(),
        err=err, ok=ok, inf=inf, arl_preview=preview,
        redirect_uri=REDIRECT_URI, public_url=PUBLIC_URL)


@app.route("/oauth/start")
def oauth_start():
    if not oauth_available():
        session["err"] = "OAuth not configured (DEEZER_APP_ID/DEEZER_APP_SECRET missing)."
        return redirect(url_for("index"))
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    return redirect(build_auth_url(state))


@app.route("/callback")
def oauth_callback():
    ret_state   = request.args.get("state", "")
    saved_state = session.pop("oauth_state", "")
    if saved_state and ret_state != saved_state:
        session["err"] = "OAuth state mismatch — possible CSRF. Please try again."
        return redirect(url_for("index"))

    err_param = request.args.get("error_reason", request.args.get("error", ""))
    if err_param:
        session["err"] = f"Deezer denied the request: {err_param}"
        return redirect(url_for("index"))

    code = request.args.get("code", "")
    if not code:
        session["err"] = "No code returned from Deezer. Please try again."
        return redirect(url_for("index"))

    logger.info("OAuth callback: exchanging code for access_token...")
    token, err = exchange_code(code)
    if not token:
        session["err"] = f"Token exchange failed: {err}"
        return redirect(url_for("index"))
    logger.info(f"Access token received (len={len(token)})")

    logger.info("Extracting ARL from Deezer session...")
    arl, err = arl_from_token(token)
    if arl:
        if not write_arl(arl):
            session["err"] = "ARL captured but could not be written to disk. Check container permissions."
            return redirect(url_for("index"))
        session["ok"]          = f"ARL extracted and saved (length: {len(arl)}). Downloads resume within 60 s."
        session["arl_preview"] = arl[:32]
        logger.info(f"Full OAuth->ARL flow complete (len={len(arl)})")
    else:
        logger.warning(f"OAuth OK but ARL not in cookies: {err}")
        session["inf"] = err

    return redirect(url_for("index"))


@app.route("/submit-arl", methods=["POST"])
def submit_arl():
    if request.form.get("secret", "") != SECRET:
        session["err"] = "Incorrect server passphrase."
        return redirect(url_for("index"))
    arl = request.form.get("arl", "").strip()
    if not arl:
        session["err"] = "ARL cannot be empty."
        return redirect(url_for("index"))
    if len(arl) < 100:
        session["err"] = f"ARL too short ({len(arl)} chars). Valid ARL is ~192 chars."
        return redirect(url_for("index"))
    if not write_arl(arl):
        session["err"] = "Could not write ARL to disk. Check container permissions."
        return redirect(url_for("index"))
    logger.info(f"Manual ARL submitted (len={len(arl)})")
    session["ok"]          = f"ARL saved (length: {len(arl)}). Downloads resume within 60 s."
    session["arl_preview"] = arl[:32]
    return redirect(url_for("index"))


@app.route("/status")
def status():
    arl = current_arl()
    return jsonify({
        "stale":           is_stale(),
        "arl_configured":  bool(arl),
        "arl_length":      len(arl) if arl else 0,
        "arl_file":        str(ARL_CONFIG_FILE),
        "arl_file_exists": ARL_CONFIG_FILE.exists(),
        "oauth_ready":     oauth_available(),
        "port_open":       _port_is_open(),
        "timestamp":       time.time(),
    })


# ---------------------------------------------------------------------------
# Main lifecycle loop
# ---------------------------------------------------------------------------

def run():
    """Main entry point — polls for stale flag and manages port lifecycle."""
    logger.info("ARL server lifecycle loop starting")
    logger.info(f"  ARL file    : {ARL_CONFIG_FILE}")
    logger.info(f"  Data dir    : {DATA_DIR}")
    logger.info(f"  Port        : {PORT}")
    logger.info(f"  OAuth ready : {oauth_available()}")
    logger.info(f"  Poll interval: {POLL_INTERVAL}s")
    logger.info(f"  Initial state: {'STALE' if is_stale() else 'OK — port closed'}")

    while True:
        stale = is_stale()

        if stale and not _port_is_open():
            logger.info("ARL stale flag detected — opening port")
            _open_port()

        elif _port_is_open():
            # Port is open; check whether we should close it
            if _shutdown_flag.is_set():
                # ARL was just written successfully
                logger.info("Shutdown flag set after successful ARL write — closing port")
                _close_port()
                _shutdown_flag.clear()
            elif not stale:
                # Flag disappeared (manual deletion or external update)
                logger.info("Stale flag cleared externally — closing port")
                _close_port()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
