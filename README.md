# SonataniVally — Notification Bridge Server

A lightweight Flask server that bridges the **PHP web application** (`index.php`) and the **Android native app** for real-time notifications and group call alerts.

**Live URL:** `https://notificationcall.render.com`

---

## 🎯 Purpose

The web app runs on InfinityFree (PHP-only, no push notification support).
The Android app needs to be alerted when:
- A new **post** is created
- A new **message** is posted in any group
- A **group audio call** is started

This server acts as a middleman:

```
┌─────────────────┐     POST /notify      ┌──────────────────────┐
│  PHP Web App    │  ───────────────────► │  Notification Bridge │
│  (index.php)    │                       │   (Flask on Render)  │
└─────────────────┘                       └──────────┬───────────┘
                                                     │
                                                     │  GET /api/poll
                                                     │  GET /api/calls/active
                                                     ▼
                                          ┌──────────────────────┐
                                          │   Android Native App │
                                          │  (Java + Volley)     │
                                          └──────────────────────┘
```

---

## 🚀 Deployment on Render

### 1. Push code to GitHub

```bash
git init
git add main.py requirements.txt Procfile README.md
git commit -m "Notification bridge server"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/sonatani-notify.git
git push -u origin main
```

### 2. Create Render Web Service

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repository
3. Fill in:
   - **Name:** `notificationcall`
   - **Environment:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   - **Plan:** Free
4. Click **Create Web Service**

Render will deploy and provide a URL like:
`https://notificationcall.onrender.com`

### 3. Custom Domain (optional)

To use `https://notificationcall.render.com`:
- Go to **Settings** → **Custom Domains** → Add `notificationcall.render.com`
- Add the CNAME record in your DNS provider as instructed

---

## 🔌 API Reference

### 1. `POST /notify`
Called by **PHP (`index.php`)** when events happen.

**Request:**
```json
{
  "type": "call",
  "group_id": 1,
  "group_name": "Sanatani General",
  "channel": "sv_group_1_1700000000",
  "started_by": 5,
  "started_by_name": "Khokon Admin",
  "time": "2025-01-01 12:00:00"
}
```

**Types supported:**
| `type` | When it fires | Extra fields |
|--------|--------------|--------------|
| `post` | User creates a post | `user_name`, `content` |
| `message` | User sends group message | `user_name`, `group_name`, `content` |
| `call` | Group audio call starts | `channel`, `group_id`, `started_by_name` |
| `call_end` | Call ends | `channel` |

**Response:**
```json
{ "ok": true, "notification_id": "uuid-..." }
```

---

### 2. `GET /api/poll`
Called by the **Android app** every 5–15 seconds.

**Query params:**
| Param | Required | Description |
|-------|----------|-------------|
| `device_id` | Yes | Unique device identifier |
| `since` | No | ISO timestamp — only return newer items |
| `type` | No | `call` / `post` / `message` / `all` (default) |

**Example:**
```
GET /api/poll?device_id=abc123&since=2025-01-01T12:00:00Z&type=all
```

**Response:**
```json
{
  "ok": true,
  "server_time": "2025-01-01T12:30:00Z",
  "count": 2,
  "notifications": [
    {
      "id": "uuid-1",
      "type": "call",
      "title": "📞 Incoming Call — Sanatani General",
      "body": "Khokon Admin started a group audio call.",
      "data": { "...original payload..." },
      "received_at": "2025-01-01T12:29:00Z",
      "is_read": false
    }
  ]
}
```

---

### 3. `GET /api/calls/active`
Android app uses this to detect ringing state.

**Response:**
```json
{
  "ok": true,
  "count": 1,
  "calls": [
    {
      "channel": "sv_group_1_1700000000",
      "group_id": 1,
      "group_name": "Sanatani General",
      "started_by": 5,
      "started_by_name": "Khokon Admin",
      "started_at": "2025-01-01T12:29:00Z",
      "is_active": true
    }
  ]
}
```

---

### 4. `POST /api/ack`
Mark notifications as read.

**Body:**
```json
{ "ids": ["uuid-1", "uuid-2"] }
```
or
```json
{ "all": true }
```

---

### 5. `POST /api/register`
Optional — Android registers device with FCM token for future push support.

**Body:**
```json
{
  "device_id": "abc123",
  "fcm_token": "optional-fcm-token",
  "user_mobile": "+8801XXXXXXXXX"
}
```

---

### 6. `GET /api/stats`
Server statistics.

```json
{
  "ok": true,
  "uptime_seconds": 3600,
  "notification_count": 42,
  "active_call_count": 1,
  "registered_devices": 3,
  "server_time": "2025-01-01T13:00:00Z"
}
```

---

### 7. `GET /health`
Render health check.

```json
{ "status": "ok", "time": "2025-01-01T13:00:00Z" }
```

---

## 🧪 Testing the Server

### Test /notify (send a fake call notification)
```bash
curl -X POST https://notificationcall.render.com/notify \
  -H "Content-Type: application/json" \
  -d '{
    "type":"call",
    "group_id":1,
    "group_name":"Test Group",
    "channel":"sv_test_123",
    "started_by":1,
    "started_by_name":"Tester"
  }'
```

### Test /api/poll (Android will call this)
```bash
curl "https://notificationcall.render.com/api/poll?device_id=test123"
```

### Test /api/calls/active
```bash
curl https://notificationcall.render.com/api/calls/active
```

---

## 📱 Android Native App (Java)

The Android app polls `/api/calls/active` every **5 seconds**.
When `count > 0`, it starts ringing with `RingtoneManager` and shows a high-priority notification.

### Permissions (AndroidManifest.xml)
```xml
<uses-permission android:name="android.permission.INTERNET"/>
<uses-permission android:name="android.permission.FOREGROUND_SERVICE"/>
<uses-permission android:name="android.permission.POST_NOTIFICATIONS"/>
<uses-permission android:name="android.permission.VIBRATE"/>
<uses-permission android:name="android.permission.WAKE_LOCK"/>
```

### Main Polling Service
See `CallPollingService.java` in the Android section below.

---

## ⚠️ Notes

- **Storage is in-memory** — Render free tier restarts periodically, so notifications are lost on restart. For production, swap `deque` with Redis or PostgreSQL.
- **Free tier cold start** — Render free services sleep after 15 min of inactivity. First request may take ~30s.
- **Rate limits** — With `--workers 1 --threads 4` this handles ~50 req/s easily.

---

## 🔄 How PHP Integrates

In `index.php`, the `post_external_notification()` function POSTs to `/notify`:

```php
function post_external_notification($payload){
    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL => 'https://notificationcall.render.com/notify',
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
        CURLOPT_POSTFIELDS => json_encode($payload),
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 5,
    ]);
    curl_exec($ch);
    curl_close($ch);
}
```

Called from:
- `create_post` → `type: 'post'`
- `send_message` → `type: 'message'`
- `start_call` → `type: 'call'`

---

## 📄 License

MIT — SonataniVally Project