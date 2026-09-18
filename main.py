"""
SonataniVally Notification Bridge Server
=========================================
Deployed at: https://notificationcall.render.com

Purpose:
  1. Receives notifications from PHP (index.php) via POST /notify
  2. Serves notifications to Android native app via GET /api/poll
  3. Tracks active group calls for Android ringing
  4. Live dashboard showing:
       - All notifications PHP sent to Python
       - All requests Android made to Python
       - What Android fetched

Flow:
  PHP (index.php)  ---POST /notify--->  This Server  <--GET /api/poll---  Android App
"""

from flask import Flask, request, jsonify, render_template_string
from datetime import datetime, timedelta
from collections import deque
from threading import Lock
import os
import uuid
import json

app = Flask(__name__)

# ============================================================
# CONFIG
# ============================================================
MAX_NOTIFICATIONS = 500
MAX_EVENTS = 300                 # Event log (both directions)
NOTIFICATION_TTL_SECONDS = 3600
CALL_TTL_SECONDS = 600
SERVER_START_TIME = datetime.utcnow()

# ============================================================
# IN-MEMORY STORAGE
# ============================================================
notifications = deque(maxlen=MAX_NOTIFICATIONS)   # All notifications
active_calls = {}                                  # channel -> call data
registered_devices = {}                            # device_id -> metadata
event_log = deque(maxlen=MAX_EVENTS)               # Both-direction event log
stats = {
    "php_posts_total": 0,
    "php_posts_by_type": {"post": 0, "message": 0, "call": 0, "call_end": 0, "unknown": 0},
    "android_polls_total": 0,
    "android_calls_polls_total": 0,
    "android_ack_total": 0,
    "android_register_total": 0,
    "android_last_poll_time": None,
    "android_last_poll_device": None,
    "android_last_poll_ip": None,
    "android_last_poll_count": 0,
    "php_last_post_time": None,
    "php_last_post_type": None,
    "php_last_post_ip": None,
}
lock = Lock()

# ============================================================
# HELPERS
# ============================================================
def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def log_event(direction, event_type, summary, extra=None, ip=None):
    """
    direction: 'php_to_python' | 'android_to_python' | 'python_to_android'
    """
    with lock:
        event_log.append({
            "id": str(uuid.uuid4())[:8],
            "direction": direction,
            "event_type": event_type,
            "summary": summary,
            "extra": extra or {},
            "ip": ip,
            "time": now_iso(),
        })

def cleanup_old():
    cutoff = datetime.utcnow() - timedelta(seconds=NOTIFICATION_TTL_SECONDS)
    with lock:
        while notifications and datetime.fromisoformat(
            notifications[0]["received_at"].replace("Z", "")
        ) < cutoff:
            notifications.popleft()

        call_cutoff = datetime.utcnow() - timedelta(seconds=CALL_TTL_SECONDS)
        stale = [
            ch for ch, c in active_calls.items()
            if datetime.fromisoformat(c["started_at"].replace("Z", "")) < call_cutoff
        ]
        for ch in stale:
            del active_calls[ch]

def get_client_ip():
    # Render uses X-Forwarded-For
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"

# ============================================================
# ROUTE: POST /notify  (called by PHP)
# ============================================================
@app.route("/notify", methods=["POST"])
def receive_notification():
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        log_event("php_to_python", "error", "Invalid JSON payload")
        return jsonify({"ok": False, "error": "Invalid JSON"}), 400

    ntype = data.get("type", "unknown")
    received_at = now_iso()
    ip = get_client_ip()

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

        # Update stats
        stats["php_posts_total"] += 1
        if ntype in stats["php_posts_by_type"]:
            stats["php_posts_by_type"][ntype] += 1
        else:
            stats["php_posts_by_type"]["unknown"] += 1
        stats["php_last_post_time"] = received_at
        stats["php_last_post_type"] = ntype
        stats["php_last_post_ip"] = ip

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

    # Log event
    log_event(
        "php_to_python",
        ntype,
        f"PHP → Python: {notif['title']}",
        extra={"body": notif["body"], "raw": data},
        ip=ip,
    )

    print(f"[PHP→PY] type={ntype} | {notif['title']} from {ip}")
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
    device_id = request.args.get("device_id", "unknown")
    since = request.args.get("since")
    filter_type = request.args.get("type", "all")
    ip = get_client_ip()

    cleanup_old()

    with lock:
        registered_devices[device_id] = {
            "last_seen": now_iso(),
            "last_ip": ip,
        }
        stats["android_polls_total"] += 1
        stats["android_last_poll_time"] = now_iso()
        stats["android_last_poll_device"] = device_id
        stats["android_last_poll_ip"] = ip

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
        stats["android_last_poll_count"] = len(results)

    # Log event
    log_event(
        "android_to_python",
        "poll",
        f"Android polled: {len(results)} item(s) returned (device={device_id[:12]})",
        extra={
            "device_id": device_id,
            "filter_type": filter_type,
            "since": since,
            "returned_count": len(results),
            "returned_ids": [n["id"][:8] for n in results[:5]],
        },
        ip=ip,
    )

    print(f"[AND→PY] poll | device={device_id[:12]} | returned={len(results)} | ip={ip}")
    return jsonify({
        "ok": True,
        "server_time": now_iso(),
        "count": len(results),
        "notifications": results,
    })

# ============================================================
# ROUTE: GET /api/calls/active
# ============================================================
@app.route("/api/calls/active", methods=["GET"])
def active_calls_endpoint():
    cleanup_old()
    ip = get_client_ip()
    device_id = request.args.get("device_id", "unknown")

    with lock:
        calls = [c for c in active_calls.values() if c.get("is_active")]
        stats["android_calls_polls_total"] += 1

    log_event(
        "android_to_python",
        "check_calls",
        f"Android checked active calls: {len(calls)} live",
        extra={"device_id": device_id, "count": len(calls)},
        ip=ip,
    )

    print(f"[AND→PY] calls/active | count={len(calls)} | ip={ip}")
    return jsonify({
        "ok": True,
        "server_time": now_iso(),
        "count": len(calls),
        "calls": calls,
    })

# ============================================================
# ROUTE: POST /api/ack
# ============================================================
@app.route("/api/ack", methods=["POST"])
def ack_notifications():
    data = request.get_json(force=True, silent=True) or {}
    ids = set(data.get("ids", []))
    all_flag = bool(data.get("all"))
    ip = get_client_ip()

    updated = 0
    with lock:
        for n in notifications:
            if all_flag or n["id"] in ids:
                if not n["is_read"]:
                    n["is_read"] = True
                    updated += 1
        stats["android_ack_total"] += 1

    log_event(
        "android_to_python",
        "ack",
        f"Android acknowledged: {updated} notification(s)",
        extra={"count": updated, "all": all_flag},
        ip=ip,
    )
    return jsonify({"ok": True, "updated": updated})

# ============================================================
# ROUTE: POST /api/register
# ============================================================
@app.route("/api/register", methods=["POST"])
def register_device():
    data = request.get_json(force=True, silent=True) or {}
    device_id = data.get("device_id")
    ip = get_client_ip()
    if not device_id:
        return jsonify({"ok": False, "error": "device_id required"}), 400

    with lock:
        registered_devices[device_id] = {
            "device_id": device_id,
            "fcm_token": data.get("fcm_token", ""),
            "user_mobile": data.get("user_mobile", ""),
            "registered_at": now_iso(),
            "last_seen": now_iso(),
            "last_ip": ip,
        }
        stats["android_register_total"] += 1

    log_event(
        "android_to_python",
        "register",
        f"Android registered device: {device_id[:12]}",
        extra=data,
        ip=ip,
    )
    return jsonify({"ok": True})

# ============================================================
# ROUTE: GET /api/stats
# ============================================================
@app.route("/api/stats", methods=["GET"])
def stats_endpoint():
    with lock:
        return jsonify({
            "ok": True,
            "uptime_seconds": int((datetime.utcnow() - SERVER_START_TIME).total_seconds()),
            "notification_count": len(notifications),
            "active_call_count": sum(1 for c in active_calls.values() if c.get("is_active")),
            "registered_devices": len(registered_devices),
            "server_time": now_iso(),
            "detailed_stats": dict(stats),
        })

# ============================================================
# ROUTE: GET /health
# ============================================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": now_iso()})

# ============================================================
# ROUTE: GET /api/events  (live event log JSON — used by dashboard)
# ============================================================
@app.route("/api/events", methods=["GET"])
def events_endpoint():
    since_id = request.args.get("since_id")
    with lock:
        events = list(event_log)
    if since_id:
        try:
            idx = next((i for i, e in enumerate(events) if e["id"] == since_id), None)
            if idx is not None:
                events = events[idx+1:]
        except Exception:
            pass
    return jsonify({
        "ok": True,
        "count": len(events),
        "server_time": now_iso(),
        "events": events[::-1],   # newest first
    })

# ============================================================
# ROUTE: GET /api/notifications  (raw notifications for dashboard)
# ============================================================
@app.route("/api/notifications", methods=["GET"])
def notifications_endpoint():
    with lock:
        items = list(notifications)[::-1]   # newest first
    return jsonify({
        "ok": True,
        "count": len(items),
        "notifications": items,
    })

# ============================================================
# ROUTE: GET /api/active_calls  (for dashboard)
# ============================================================
@app.route("/api/active_calls", methods=["GET"])
def active_calls_dashboard():
    with lock:
        calls = list(active_calls.values())
    return jsonify({"ok": True, "count": len(calls), "calls": calls})

# ============================================================
# ROUTE: GET /api/devices  (registered devices)
# ============================================================
@app.route("/api/devices", methods=["GET"])
def devices_endpoint():
    with lock:
        devices = list(registered_devices.values())
    return jsonify({"ok": True, "count": len(devices), "devices": devices})

# ============================================================
# ROUTE: GET /  — LIVE DASHBOARD
# ============================================================
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SonataniVally Notification Bridge — Live Dashboard</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: radial-gradient(circle at 20% 10%, #1a2540 0%, #0d1424 60%);
    color: #eaf0ff; min-height: 100vh; padding: 20px;
    background-attachment: fixed;
  }
  .wrap { max-width: 1240px; margin: 0 auto; }

  /* Header */
  .header {
    display: flex; align-items: center; justify-content: space-between;
    background: rgba(26, 37, 64, 0.7); backdrop-filter: blur(20px);
    border: 1px solid rgba(110, 150, 255, 0.25);
    border-radius: 20px; padding: 22px 26px; margin-bottom: 18px;
    box-shadow: 0 12px 40px rgba(0,0,0,0.4);
    flex-wrap: wrap; gap: 14px;
  }
  .brand { display: flex; align-items: center; gap: 14px; }
  .brand-icon {
    width: 52px; height: 52px; border-radius: 16px;
    background: linear-gradient(135deg, #5a8aff, #3a62d8);
    display: flex; align-items: center; justify-content: center;
    font-size: 24px; box-shadow: 0 6px 20px rgba(90,138,255,0.5);
  }
  .brand-text h1 { font-size: 20px; font-weight: 800; color: #7ba2ff; letter-spacing: 0.3px; }
  .brand-text p { font-size: 12px; color: #8a9ac7; margin-top: 3px; }
  .live-badge {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 8px 16px; border-radius: 12px;
    background: rgba(16,185,129,0.15); border: 1px solid #10b981;
    color: #6ee7b7; font-size: 12px; font-weight: 700;
    letter-spacing: 0.5px; text-transform: uppercase;
  }
  .live-dot {
    width: 8px; height: 8px; background: #10b981;
    border-radius: 50%; box-shadow: 0 0 8px #10b981;
    animation: pulse 1.5s infinite;
  }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }

  /* Stats Grid */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px; margin-bottom: 18px;
  }
  .stat {
    background: rgba(26, 37, 64, 0.7); backdrop-filter: blur(14px);
    border: 1px solid rgba(110, 150, 255, 0.2);
    border-radius: 16px; padding: 18px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3);
    transition: transform 0.2s, border-color 0.2s;
  }
  .stat:hover { transform: translateY(-2px); border-color: rgba(110,150,255,0.5); }
  .stat .icon {
    width: 38px; height: 38px; border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 18px; margin-bottom: 12px;
  }
  .stat .icon.php { background: rgba(239,68,99,0.18); color: #f87171; }
  .stat .icon.android { background: rgba(16,185,129,0.18); color: #6ee7b7; }
  .stat .icon.call { background: rgba(90,138,255,0.18); color: #7ba2ff; }
  .stat .icon.device { background: rgba(251,146,60,0.18); color: #fbbf24; }
  .stat .value { font-size: 28px; font-weight: 900; color: #eaf0ff; line-height: 1.1; }
  .stat .label { font-size: 11px; color: #8a9ac7; text-transform: uppercase; letter-spacing: 1px; margin-top: 6px; font-weight: 700; }
  .stat .sub { font-size: 11px; color: #62709a; margin-top: 4px; }

  /* Panels */
  .panels { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }
  @media (max-width: 900px) { .panels { grid-template-columns: 1fr; } }
  .panel {
    background: rgba(26, 37, 64, 0.7); backdrop-filter: blur(14px);
    border: 1px solid rgba(110, 150, 255, 0.2);
    border-radius: 18px; padding: 20px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.3);
    display: flex; flex-direction: column;
    max-height: 540px;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between;
    padding-bottom: 14px; margin-bottom: 12px;
    border-bottom: 1px solid rgba(110,150,255,0.15);
  }
  .panel-title {
    font-size: 14px; font-weight: 800; color: #eaf0ff;
    display: flex; align-items: center; gap: 10px;
  }
  .panel-title .dot { width: 8px; height: 8px; border-radius: 50%; }
  .panel-title .dot.red { background: #ef4463; box-shadow: 0 0 8px #ef4463; }
  .panel-title .dot.green { background: #10b981; box-shadow: 0 0 8px #10b981; }
  .panel-title .dot.blue { background: #5a8aff; box-shadow: 0 0 8px #5a8aff; }
  .panel-title .count { font-size: 11px; color: #8a9ac7; font-weight: 600; }

  /* Buttons */
  .btn-group { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .btn {
    padding: 8px 14px; border-radius: 10px; border: none;
    font-size: 12px; font-weight: 700; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
    transition: all 0.2s; font-family: inherit;
  }
  .btn-primary { background: linear-gradient(135deg, #5a8aff, #3a62d8); color: #fff; box-shadow: 0 4px 14px rgba(90,138,255,0.4); }
  .btn-primary:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(90,138,255,0.6); }
  .btn-success { background: linear-gradient(135deg, #10b981, #059669); color: #fff; box-shadow: 0 4px 14px rgba(16,185,129,0.4); }
  .btn-success:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(16,185,129,0.6); }
  .btn-danger { background: linear-gradient(135deg, #ef4463, #b91c1c); color: #fff; box-shadow: 0 4px 14px rgba(239,68,99,0.4); }
  .btn-danger:hover { transform: translateY(-1px); box-shadow: 0 6px 20px rgba(239,68,99,0.6); }
  .btn-ghost { background: rgba(110,150,255,0.1); color: #8a9ac7; border: 1px solid rgba(110,150,255,0.2); }
  .btn-ghost:hover { background: rgba(110,150,255,0.2); color: #eaf0ff; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none !important; }

  /* Trigger button — big prominent */
  .trigger-panel {
    background: linear-gradient(135deg, rgba(90,138,255,0.15), rgba(139,92,246,0.1));
    border: 1.5px solid rgba(90,138,255,0.4);
    border-radius: 18px; padding: 22px; margin-bottom: 18px;
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 16px;
    box-shadow: 0 8px 32px rgba(90,138,255,0.2);
  }
  .trigger-info h3 { font-size: 16px; font-weight: 800; color: #7ba2ff; margin-bottom: 6px; }
  .trigger-info p { font-size: 12.5px; color: #8a9ac7; line-height: 1.6; max-width: 560px; }
  .trigger-actions { display: flex; gap: 10px; flex-wrap: wrap; }
  .big-btn {
    padding: 14px 24px; font-size: 13px; font-weight: 800;
    border-radius: 14px; border: none; cursor: pointer;
    display: inline-flex; align-items: center; gap: 10px;
    transition: all 0.22s; font-family: inherit;
    letter-spacing: 0.3px;
  }
  .big-btn.blue { background: linear-gradient(135deg, #5a8aff, #3a62d8); color: #fff; box-shadow: 0 6px 24px rgba(90,138,255,0.5); }
  .big-btn.blue:hover { transform: translateY(-2px); box-shadow: 0 10px 32px rgba(90,138,255,0.7); }
  .big-btn.green { background: linear-gradient(135deg, #10b981, #059669); color: #fff; box-shadow: 0 6px 24px rgba(16,185,129,0.5); }
  .big-btn.green:hover { transform: translateY(-2px); box-shadow: 0 10px 32px rgba(16,185,129,0.7); }
  .big-btn.orange { background: linear-gradient(135deg, #f59e0b, #d97706); color: #fff; box-shadow: 0 6px 24px rgba(245,158,11,0.5); }
  .big-btn.orange:hover { transform: translateY(-2px); box-shadow: 0 10px 32px rgba(245,158,11,0.7); }
  .big-btn:active { transform: translateY(0); }

  /* Event List */
  .list { flex: 1; overflow-y: auto; padding-right: 6px; }
  .list::-webkit-scrollbar { width: 5px; }
  .list::-webkit-scrollbar-track { background: transparent; }
  .list::-webkit-scrollbar-thumb { background: rgba(110,150,255,0.3); border-radius: 4px; }

  .event {
    padding: 12px 14px; border-radius: 12px; margin-bottom: 8px;
    background: rgba(13, 20, 36, 0.6);
    border-left: 3px solid #5a8aff;
    animation: slideIn 0.35s ease;
    transition: all 0.2s;
  }
  .event:hover { background: rgba(13, 20, 36, 0.9); transform: translateX(3px); }
  .event.php { border-left-color: #ef4463; }
  .event.android { border-left-color: #10b981; }
  .event.call { border-left-color: #f59e0b; }
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-8px); }
    to { opacity: 1; transform: translateY(0); }
  }
  .event-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
  .event-badge {
    padding: 2px 8px; border-radius: 6px; font-size: 9.5px;
    font-weight: 800; letter-spacing: 0.7px; text-transform: uppercase;
  }
  .badge-php { background: rgba(239,68,99,0.2); color: #f87171; }
  .badge-android { background: rgba(16,185,129,0.2); color: #6ee7b7; }
  .badge-call { background: rgba(245,158,11,0.2); color: #fbbf24; }
  .event-type {
    font-size: 10.5px; color: #8a9ac7; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .event-time { margin-left: auto; font-size: 10.5px; color: #62709a; font-family: monospace; }
  .event-summary { font-size: 13px; color: #eaf0ff; font-weight: 600; line-height: 1.4; word-break: break-word; }
  .event-extra {
    font-size: 11px; color: #8a9ac7; margin-top: 6px;
    font-family: 'SF Mono', Consolas, monospace;
    background: rgba(0,0,0,0.3); padding: 6px 10px; border-radius: 8px;
    word-break: break-all; max-height: 80px; overflow-y: auto;
    white-space: pre-wrap;
  }
  .event-ip { font-size: 10.5px; color: #62709a; margin-top: 4px; font-family: monospace; }

  /* Notification item */
  .notif {
    padding: 12px 14px; border-radius: 12px; margin-bottom: 8px;
    background: rgba(13,20,36,0.6); border: 1px solid rgba(110,150,255,0.15);
    animation: slideIn 0.35s ease;
  }
  .notif-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .notif-type {
    padding: 3px 10px; border-radius: 8px; font-size: 10px;
    font-weight: 800; letter-spacing: 0.7px; text-transform: uppercase;
  }
  .type-call { background: rgba(245,158,11,0.2); color: #fbbf24; }
  .type-message { background: rgba(90,138,255,0.2); color: #7ba2ff; }
  .type-post { background: rgba(139,92,246,0.2); color: #a78bfa; }
  .type-call_end { background: rgba(239,68,99,0.2); color: #f87171; }
  .type-unknown { background: rgba(138,154,199,0.2); color: #8a9ac7; }
  .notif-time { margin-left: auto; font-size: 10.5px; color: #62709a; font-family: monospace; }
  .notif-title { font-size: 13px; font-weight: 700; color: #eaf0ff; margin-bottom: 3px; }
  .notif-body { font-size: 12px; color: #8a9ac7; line-height: 1.5; }

  .empty {
    padding: 40px 20px; text-align: center; color: #62709a;
    font-size: 13px; line-height: 1.6;
  }
  .empty-icon {
    font-size: 40px; opacity: 0.3; margin-bottom: 12px; display: block;
  }

  /* Footer */
  .footer {
    text-align: center; padding: 20px; color: #62709a; font-size: 11.5px;
    border-top: 1px solid rgba(110,150,255,0.1); margin-top: 20px;
  }
  .footer code {
    font-family: 'SF Mono', Consolas, monospace;
    background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px;
    color: #7ba2ff;
  }

  /* Toast */
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    padding: 14px 22px; border-radius: 14px;
    font-size: 13px; font-weight: 600; color: #fff;
    box-shadow: 0 10px 40px rgba(0,0,0,0.5);
    z-index: 9999;
    animation: slideIn 0.3s ease;
    display: flex; align-items: center; gap: 10px;
  }
  .toast.success { background: linear-gradient(135deg, #10b981, #059669); }
  .toast.error { background: linear-gradient(135deg, #ef4463, #b91c1c); }
  .toast.info { background: linear-gradient(135deg, #5a8aff, #3a62d8); }
</style>
</head>
<body>
<div class="wrap">

  <!-- Header -->
  <div class="header">
    <div class="brand">
      <div class="brand-icon">🕉️</div>
      <div class="brand-text">
        <h1>SonataniVally Notification Bridge</h1>
        <p>PHP ↔ Python ↔ Android · Live Monitoring Dashboard</p>
      </div>
    </div>
    <div class="live-badge">
      <span class="live-dot"></span>
      <span>LIVE · Auto-refresh 3s</span>
    </div>
  </div>

  <!-- Stats Grid -->
  <div class="stats-grid">
    <div class="stat">
      <div class="icon php">📥</div>
      <div class="value" id="statPhpTotal">0</div>
      <div class="label">PHP → Python</div>
      <div class="sub">Total notifications received</div>
    </div>
    <div class="stat">
      <div class="icon call">📞</div>
      <div class="value" id="statActiveCalls">0</div>
      <div class="label">Active Calls</div>
      <div class="sub">Currently live</div>
    </div>
    <div class="stat">
      <div class="icon android">📤</div>
      <div class="value" id="statAndroidPolls">0</div>
      <div class="label">Android Polls</div>
      <div class="sub">Requests from Android app</div>
    </div>
    <div class="stat">
      <div class="icon device">📱</div>
      <div class="value" id="statDevices">0</div>
      <div class="label">Registered Devices</div>
      <div class="sub">Unique Android devices</div>
    </div>
  </div>

  <!-- Trigger Panel -->
  <div class="trigger-panel">
    <div class="trigger-info">
      <h3>🚀 Test Notification Sender</h3>
      <p>Press any button below to simulate what <code>index.php</code> sends to this server. Immediately visible in the event log below and to any connected Android device.</p>
    </div>
    <div class="trigger-actions">
      <button class="big-btn blue" onclick="sendTest('call')">📞 Send Test Call</button>
      <button class="big-btn green" onclick="sendTest('message')">💬 Send Test Message</button>
      <button class="big-btn orange" onclick="sendTest('post')">📝 Send Test Post</button>
      <button class="big-btn" style="background:linear-gradient(135deg,#8a9ac7,#62709a);color:#fff;" onclick="sendTest('call_end')">🔚 End Call</button>
    </div>
  </div>

  <!-- Two Column Panels -->
  <div class="panels">
    <!-- PHP → Python -->
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          <span class="dot red"></span>
          <span>PHP → Python</span>
          <span class="count" id="phpCount">(0)</span>
        </div>
        <button class="btn btn-ghost" onclick="clearLog('php')">Clear</button>
      </div>
      <div class="list" id="phpLog">
        <div class="empty">
          <span class="empty-icon">📥</span>
          Waiting for PHP to send notifications...<br>
          <span style="font-size:11px;">When index.php calls <code>/notify</code>, events will appear here.</span>
        </div>
      </div>
    </div>

    <!-- Python → Android -->
    <div class="panel">
      <div class="panel-head">
        <div class="panel-title">
          <span class="dot green"></span>
          <span>Python ← Android</span>
          <span class="count" id="androidCount">(0)</span>
        </div>
        <button class="btn btn-ghost" onclick="clearLog('android')">Clear</button>
      </div>
      <div class="list" id="androidLog">
        <div class="empty">
          <span class="empty-icon">📱</span>
          Waiting for Android to poll...<br>
          <span style="font-size:11px;">When the Android app calls <code>/api/poll</code> or <code>/api/calls/active</code>, events will appear here.</span>
        </div>
      </div>
    </div>
  </div>

  <!-- Recent Notifications Panel -->
  <div class="panel" style="max-height: 480px;">
    <div class="panel-head">
      <div class="panel-title">
        <span class="dot blue"></span>
        <span>Recent Notifications (raw queue)</span>
        <span class="count" id="notifCount">(0)</span>
      </div>
      <button class="btn btn-ghost" onclick="location.reload()">Refresh</button>
    </div>
    <div class="list" id="notifList">
      <div class="empty">
        <span class="empty-icon">📭</span>
        No notifications yet.
      </div>
    </div>
  </div>

  <div class="footer">
    SonataniVally Notification Bridge v2.0 · Deployed on Render ·
    API: <code>/notify</code> · <code>/api/poll</code> · <code>/api/calls/active</code> · <code>/api/stats</code> · <code>/health</code>
  </div>
</div>

<script>
const API_BASE = "";
let phpEvents = [];
let androidEvents = [];
let allEvents = [];
let lastEventId = null;
let notifyCount = 0;

// ============================================================
// TOAST
// ============================================================
function toast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transition = 'opacity 0.4s';
    setTimeout(() => t.remove(), 400);
  }, 2400);
}

// ============================================================
// FORMAT
// ============================================================
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function shortTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    const pad = n => String(n).padStart(2, '0');
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  } catch(e) { return iso; }
}

function formatExtra(obj) {
  if (!obj || typeof obj !== 'object') return '';
  try {
    return JSON.stringify(obj, null, 0).slice(0, 240);
  } catch(e) { return String(obj).slice(0, 240); }
}

// ============================================================
// RENDER
// ============================================================
function renderEvents() {
  // PHP → Python
  const phpLog = document.getElementById('phpLog');
  if (phpEvents.length === 0) {
    phpLog.innerHTML = `<div class="empty">
      <span class="empty-icon">📥</span>
      Waiting for PHP to send notifications...<br>
      <span style="font-size:11px;">When index.php calls <code>/notify</code>, events will appear here.</span>
    </div>`;
  } else {
    phpLog.innerHTML = phpEvents.map(renderEventItem).join('');
  }
  document.getElementById('phpCount').textContent = '(' + phpEvents.length + ')';

  // Android → Python
  const androidLog = document.getElementById('androidLog');
  if (androidEvents.length === 0) {
    androidLog.innerHTML = `<div class="empty">
      <span class="empty-icon">📱</span>
      Waiting for Android to poll...<br>
      <span style="font-size:11px;">When the Android app calls <code>/api/poll</code> or <code>/api/calls/active</code>, events will appear here.</span>
    </div>`;
  } else {
    androidLog.innerHTML = androidEvents.map(renderEventItem).join('');
  }
  document.getElementById('androidCount').textContent = '(' + androidEvents.length + ')';
}

function renderEventItem(ev) {
  const isPhp = ev.direction === 'php_to_python';
  const isCall = ev.event_type === 'call' || ev.event_type === 'call_end';
  const cls = isPhp ? 'php' : (isCall ? 'call' : 'android');
  const badgeCls = isPhp ? 'badge-php' : (isCall ? 'badge-call' : 'badge-android');
  const badgeText = isPhp ? 'PHP' : 'ANDROID';

  let extra = '';
  if (ev.extra && Object.keys(ev.extra).length > 0) {
    extra = `<div class="event-extra">${esc(formatExtra(ev.extra))}</div>`;
  }
  let ip = ev.ip ? `<div class="event-ip">IP: ${esc(ev.ip)}</div>` : '';

  return `<div class="event ${cls}">
    <div class="event-head">
      <span class="event-badge ${badgeCls}">${badgeText}</span>
      <span class="event-type">${esc(ev.event_type)}</span>
      <span class="event-time">${esc(shortTime(ev.time))}</span>
    </div>
    <div class="event-summary">${esc(ev.summary)}</div>
    ${extra}
    ${ip}
  </div>`;
}

async function loadNotifications() {
  try {
    const r = await fetch(API_BASE + '/api/notifications');
    const j = await r.json();
    const list = document.getElementById('notifList');
    if (!j.notifications || j.notifications.length === 0) {
      list.innerHTML = `<div class="empty"><span class="empty-icon">📭</span>No notifications yet.</div>`;
      document.getElementById('notifCount').textContent = '(0)';
      return;
    }
    if (j.count > notifyCount && notifyCount > 0) {
      toast('New notification received!', 'success');
    }
    notifyCount = j.count;
    document.getElementById('notifCount').textContent = '(' + j.count + ')';
    list.innerHTML = j.notifications.slice(0, 30).map(n => {
      const typeCls = 'type-' + (n.type || 'unknown').replace(/[^a-z_]/g, '');
      const extraText = n.data ? formatExtra(n.data) : '';
      return `<div class="notif">
        <div class="notif-head">
          <span class="notif-type ${typeCls}">${esc(n.type || 'unknown')}</span>
          <span class="notif-time">${esc(shortTime(n.received_at))}</span>
        </div>
        <div class="notif-title">${esc(n.title)}</div>
        <div class="notif-body">${esc(n.body)}</div>
        ${extraText ? `<div class="event-extra" style="margin-top:6px;">${esc(extraText)}</div>` : ''}
      </div>`;
    }).join('');
  } catch(e) {
    console.error('Failed to load notifications:', e);
  }
}

async function loadStats() {
  try {
    const r = await fetch(API_BASE + '/api/stats');
    const j = await r.json();
    const s = j.detailed_stats || {};
    document.getElementById('statPhpTotal').textContent = s.php_posts_total || 0;
    document.getElementById('statActiveCalls').textContent = j.active_call_count || 0;
    document.getElementById('statAndroidPolls').textContent =
      (s.android_polls_total || 0) + (s.android_calls_polls_total || 0);
    document.getElementById('statDevices').textContent = j.registered_devices || 0;
  } catch(e) {
    console.error('Failed to load stats:', e);
  }
}

async function loadEvents() {
  try {
    const url = lastEventId
      ? API_BASE + '/api/events?since_id=' + encodeURIComponent(lastEventId)
      : API_BASE + '/api/events';
    const r = await fetch(url);
    const j = await r.json();
    if (j.events && j.events.length > 0) {
      // Newest first from server, but we prepend in order
      for (const ev of j.events) {
        allEvents.push(ev);
      }
      // Dedupe by id
      const seen = new Set();
      allEvents = allEvents.filter(e => {
        if (seen.has(e.id)) return false;
        seen.add(e.id);
        return true;
      });
      // Keep newest first
      allEvents.sort((a, b) => (b.time || '').localeCompare(a.time || ''));
      allEvents = allEvents.slice(0, 200);

      phpEvents = allEvents.filter(e => e.direction === 'php_to_python');
      androidEvents = allEvents.filter(e => e.direction === 'android_to_python');
      renderEvents();

      // Update lastEventId to most recent (events returned newest first)
      if (j.events[0] && j.events[0].id) {
        lastEventId = j.events[0].id;
      }
    }
  } catch(e) {
    console.error('Failed to load events:', e);
  }
}

function clearLog(which) {
  if (which === 'php') {
    phpEvents = [];
    allEvents = allEvents.filter(e => e.direction !== 'php_to_python');
  } else {
    androidEvents = [];
    allEvents = allEvents.filter(e => e.direction !== 'android_to_python');
  }
  renderEvents();
  toast('Log cleared (view only — server keeps full history)', 'info');
}

// ============================================================
// TEST SENDER
// ============================================================
async function sendTest(type) {
  const now = new Date();
  const stamp = now.getTime();
  let payload;

  if (type === 'call') {
    payload = {
      type: 'call',
      group_id: 1,
      group_name: 'Test Group ' + stamp.toString().slice(-4),
      channel: 'sv_test_' + stamp,
      started_by: 1,
      started_by_name: 'Admin Tester',
      time: now.toISOString()
    };
  } else if (type === 'call_end') {
    payload = {
      type: 'call_end',
      channel: 'sv_test_' + (stamp - 5000),
      time: now.toISOString()
    };
  } else if (type === 'message') {
    payload = {
      type: 'message',
      group_id: 1,
      group_name: 'Test Group',
      user_name: 'Test User',
      content: 'Hello from dashboard test @ ' + now.toLocaleTimeString(),
      time: now.toISOString()
    };
  } else {
    payload = {
      type: 'post',
      user_name: 'Test Poster',
      content: 'Test post from dashboard @ ' + now.toLocaleTimeString(),
      time: now.toISOString()
    };
  }

  try {
    const r = await fetch('/notify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const j = await r.json();
    if (j.ok) {
      toast('✅ Test ' + type + ' sent! Watch the log →', 'success');
    } else {
      toast('❌ Failed: ' + (j.error || 'unknown'), 'error');
    }
  } catch(e) {
    toast('❌ Network error: ' + e.message, 'error');
  }
}

// ============================================================
// POLLING LOOP
// ============================================================
loadStats();
loadEvents();
loadNotifications();

setInterval(loadStats, 3000);
setInterval(loadEvents, 3000);
setInterval(loadNotifications, 3000);
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(DASHBOARD_HTML)

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)