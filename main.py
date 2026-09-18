"""
SonataniVally Notification Bridge Server
=========================================
Deployed at: https://notificationcall.render.com
Purpose:
  1. Receives notifications from PHP (index.php) via POST /notify
  2. Serves notifications to Android native app via GET /api/poll
  3. Tracks active group calls for Android ringing
  4. Provides acknowledge/clear endpoints

Flow:
  PHP (index.php)  ---POST /notify--->  This Server  <--GET /api/poll---  Android App
"""

from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timedelta
from collections import deque
from threading import Lock
import os
import uuid

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================
MAX_NOTIFICATIONS = 500          # Keep last N notifications in memory
NOTIFICATION_TTL_SECONDS = 3600  # Auto-delete after 1 hour
CALL_TTL_SECONDS = 600           # Active call expires after 10 min of inactivity
SERVER_START_TIME = datetime.utcnow()

# ============================================================
# IN-MEMORY STORAGE
# ============================================================
# Note: Render free tier restarts periodically, so storage is ephemeral.
# For production, replace with Redis or a database.
notifications = deque(maxlen=MAX_NOTIFICATIONS)   # All notifications
active_calls = {}                                  # channel -> call data
registered_devices = {}                            # device_id -> metadata
lock = Lock()

# ============================================================
# HELPERS
# ============================================================
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def cleanup_old():
    """Remove expired notifications and stale calls."""
    cutoff = datetime.utcnow() - timedelta(seconds=NOTIFICATION_TTL_SECONDS)
    with lock:
        # Trim expired notifications
        while notifications and datetime.fromisoformat(
            notifications[0]["received_at"].replace("Z", "")
        ) < cutoff:
            notifications.popleft()

        # Trim stale calls
        call_cutoff = datetime.utcnow() - timedelta(seconds=CALL_TTL_SECONDS)
        stale = [
            ch for ch, c in active_calls.items()
            if datetime.fromisoformat(c["started_at"].replace("Z", "")) < call_cutoff
        ]
        for ch in stale:
            del active_calls[ch]

# ============================================================
# ROUTE: POST /notify  (called by PHP)
# ============================================================
@app.route("/notify", methods=["POST"])
def receive_notification():
    """
    Receives a notification from index.php.
    Expected JSON body (from PHP):
      {
        "type": "post" | "message" | "call" | "call_end",
        "user_name": "...",
        "content": "...",
        "group_id": 1,          (for message/call)
        "group_name": "...",
        "channel": "...",       (for call)
        "started_by": 5,
        "started_by_name": "...",
        "time": "2025-01-01 12:00:00"
      }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    ntype = data.get("type", "unknown")
    received_at = now_iso()

    # Build a normalized notification object
    notif = {
        "id": str(uuid.uuid4()),
        "type": ntype,
        "title": _build_title(data),
        "body": _build_body(data),
        "data": data,
        "received_at": received_at,
        "is_read": False,
    }

    with lock:
        notifications.append(notif)

        # Handle call tracking
        if ntype == "call":
            channel = data.get("channel") or f"call_{data.get('group_id', 'x')}"
            active_calls[channel] = {
                "channel": channel,
                "group_id": data.get("group_id"),
                "group_name": data.get("group_name", "Group"),
                "started_by": data.get("started_by"),
                "started_by_name": data.get("started_by_name", "Someone"),
                "started_at": received_at,
                "is_active": True,
            }
        elif ntype == "call_end":
            channel = data.get("channel")
            if channel and channel in active_calls:
                active_calls[channel]["is_active"] = False
                del active_calls[channel]

    print(f"[NOTIFY] type={ntype} | {notif['title']} — {notif['body']}")
    return jsonify({"ok": True, "notification_id": notif["id"]})

def _build_title(data):
    t = data.get("type", "")
    if t == "call":
        return f"📞 Incoming Call — {data.get('group_name', 'Group')}"
    if t == "call_end":
        return "Call Ended"
    if t == "post":
        return f"📝 New Post by {data.get('user_name', 'Someone')}"
    if t == "message":
        return f"💬 {data.get('user_name', 'Someone')} in {data.get('group_name', 'Group')}"
    return "SonataniVally Notification"

def _build_body(data):
    t = data.get("type", "")
    if t == "call":
        return f"{data.get('started_by_name', 'Someone')} started a group audio call."
    if t == "post":
        return (data.get("content") or "")[:120]
    if t == "message":
        return (data.get("content") or "")[:120]
    return str(data)[:120]

# ============================================================
# ROUTE: GET /api/poll  (called by Android app)
# ============================================================
@app.route("/api/poll", methods=["GET"])
def poll_notifications():
    """
    Android app polls this endpoint.
    Query params:
      - device_id (required): unique device ID
      - since (optional): ISO timestamp; only return items after this
      - type (optional): filter by type ('call', 'post', 'message', 'all')
    Response:
      {
        ok: true,
        server_time: "...",
        notifications: [ { id, type, title, body, data, received_at, is_read } ]
      }
    """
    device_id = request.args.get("device_id", "unknown")
    since = request.args.get("since")
    filter_type = request.args.get("type", "all")

    cleanup_old()

    # Register device (light touch)
    with lock:
        registered_devices[device_id] = {
            "last_seen": now_iso(),
            "last_ip": request.remote_addr,
        }

    # Filter notifications
    results = []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", ""))
        except Exception:
            since_dt = None

    with lock:
        for n in notifications:
            if filter_type != "all" and n["type"] != filter_type:
                continue
            if since_dt:
                n_dt = datetime.fromisoformat(n["received_at"].replace("Z", ""))
                if n_dt <= since_dt:
                    continue
            results.append(n)

    return jsonify({
        "ok": True,
        "server_time": now_iso(),
        "count": len(results),
        "notifications": results,
    })

# ============================================================
# ROUTE: GET /api/calls/active  (called by Android for ringing)
# ============================================================
@app.route("/api/calls/active", methods=["GET"])
def active_calls_endpoint():
    """
    Returns active group calls only.
    Android uses this to start ringing when a call exists.
    """
    cleanup_old()
    with lock:
        calls = [c for c in active_calls.values() if c.get("is_active")]
    return jsonify({
        "ok": True,
        "server_time": now_iso(),
        "count": len(calls),
        "calls": calls,
    })

# ============================================================
# ROUTE: POST /api/ack  (Android marks notifications as read)
# ============================================================
@app.route("/api/ack", methods=["POST"])
def ack_notifications():
    """
    Mark notifications as read.
    Body JSON:
      { "ids": ["id1","id2"] }   OR   { "all": true }
    """
    data = request.get_json(force=True, silent=True) or {}
    ids = set(data.get("ids", []))
    all_flag = bool(data.get("all"))

    updated = 0
    with lock:
        for n in notifications:
            if all_flag or n["id"] in ids:
                if not n["is_read"]:
                    n["is_read"] = True
                    updated += 1

    return jsonify({"ok": True, "updated": updated})

# ============================================================
# ROUTE: POST /api/register  (optional — for FCM push tokens)
# ============================================================
@app.route("/api/register", methods=["POST"])
def register_device():
    """
    Android app may register a device with an optional FCM token.
    Body JSON:
      { "device_id": "...", "fcm_token": "...", "user_mobile": "..." }
    """
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    with lock:
        registered_devices[device_id] = {
            "device_id": device_id,
            "fcm_token": data.get("fcm_token", ""),
            "user_mobile": data.get("user_mobile", ""),
            "registered_at": now_iso(),
            "last_seen": now_iso(),
            "last_ip": request.remote_addr,
        }
    return jsonify({"ok": True})

# ============================================================
# ROUTE: GET /api/stats  (admin/monitoring)
# ============================================================
@app.route("/api/stats", methods=["GET"])
def stats():
    with lock:
        return jsonify({
            "ok": True,
            "uptime_seconds": int((datetime.utcnow() - SERVER_START_TIME).total_seconds()),
            "notification_count": len(notifications),
            "active_call_count": sum(1 for c in active_calls.values() if c.get("is_active")),
            "registered_devices": len(registered_devices),
            "server_time": now_iso(),
        })

# ============================================================
# ROUTE: GET /health  (Render health check)
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": now_iso()})

# ============================================================
# ROUTE: GET /  (info page)
# ============================================================
INFO_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SonataniVally Notification Bridge</title>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #0d1424, #1a2540);
    color: #eaf0ff; min-height: 100vh; margin: 0;
    display: flex; align-items: center; justify-content: center; padding: 20px;
  }
  .card {
    background: rgba(26, 37, 64, 0.85);
    border: 1px solid rgba(110, 150, 255, 0.25);
    border-radius: 24px; padding: 40px; max-width: 720px; width: 100%;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(20px);
  }
  h1 { font-size: 28px; margin: 0 0 8px; color: #7ba2ff; }
  .tag { display: inline-block; padding: 4px 12px; background: rgba(16,185,129,0.15); border: 1px solid #10b981; color: #6ee7b7; border-radius: 12px; font-size: 12px; font-weight: 700; margin-bottom: 20px; }
  .desc { color: #8a9ac7; font-size: 14px; line-height: 1.7; margin-bottom: 28px; }
  .endpoints { display: flex; flex-direction: column; gap: 10px; }
  .ep { display: flex; align-items: center; gap: 12px; padding: 14px 16px; background: rgba(13,20,36,0.6); border-radius: 14px; border: 1px solid rgba(110,150,255,0.15); }
  .method { font-family: monospace; font-weight: 800; font-size: 11px; padding: 4px 10px; border-radius: 8px; letter-spacing: 0.5px; }
  .post { background: #ef4463; color: #fff; }
  .get { background: #10b981; color: #fff; }
  .path { font-family: 'SF Mono', Consolas, monospace; font-size: 13px; color: #eaf0ff; flex: 1; }
  .desc-ep { font-size: 11px; color: #8a9ac7; }
  .footer { margin-top: 30px; padding-top: 20px; border-top: 1px solid rgba(110,150,255,0.15); color: #62709a; font-size: 12px; text-align: center; }
</style>
</head>
<body>
  <div class="card">
    <h1>SonataniVally Notification Bridge</h1>
    <div class="tag">● RUNNING</div>
    <p class="desc">
      Bridge server between PHP web app (index.php) and the Android native app.
      PHP sends notifications to <code>/notify</code>, Android polls <code>/api/poll</code>
      and <code>/api/calls/active</code> for real-time updates.
    </p>
    <div class="endpoints">
      <div class="ep"><span class="method post">POST</span><span class="path">/notify</span><span class="desc-ep">From PHP</span></div>
      <div class="ep"><span class="method get">GET</span><span class="path">/api/poll</span><span class="desc-ep">Android polling</span></div>
      <div class="ep"><span class="method get">GET</span><span class="path">/api/calls/active</span><span class="desc-ep">Live call detection</span></div>
      <div class="ep"><span class="method post">POST</span><span class="path">/api/ack</span><span class="desc-ep">Mark read</span></div>
      <div class="ep"><span class="method post">POST</span><span class="path">/api/register</span><span class="desc-ep">Device register</span></div>
      <div class="ep"><span class="method get">GET</span><span class="path">/api/stats</span><span class="desc-ep">Server stats</span></div>
      <div class="ep"><span class="method get">GET</span><span class="path">/health</span><span class="desc-ep">Health check</span></div>
    </div>
    <div class="footer">SonataniVally · Notification Bridge v1.0</div>
  </div>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INFO_HTML)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)