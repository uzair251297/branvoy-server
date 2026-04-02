"""
LeadPro API Server
------------------
Deploy this on Railway or Render (free tier).
All your customers' scraper apps talk to this server.
You manage everything from the /admin dashboard.
"""

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import sqlite3, hashlib, secrets, time, json, re, os
from datetime import datetime
import httpx

app = Flask(__name__, static_folder="static")
CORS(app)

DB = "leadpro.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")  # Set in Railway env vars

# ─── DATABASE SETUP ───────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            active INTEGER DEFAULT 1,
            plan TEXT DEFAULT 'basic',
            scrapes_used INTEGER DEFAULT 0,
            scrapes_limit INTEGER DEFAULT 500,
            created_at TEXT DEFAULT (datetime('now')),
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS scrape_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT NOT NULL,
            query TEXT,
            location TEXT,
            results_count INTEGER DEFAULT 0,
            timestamp TEXT DEFAULT (datetime('now')),
            ip TEXT
        );

        CREATE TABLE IF NOT EXISTS admin_sessions (
            token TEXT PRIMARY KEY,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)

init_db()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def generate_key():
    return "lp_" + secrets.token_urlsafe(32)

def validate_key(api_key):
    """Check if key exists and is active and under limit."""
    with get_db() as db:
        user = db.execute(
            "SELECT * FROM users WHERE api_key=? AND active=1", (api_key,)
        ).fetchone()
        if not user:
            return None, "Invalid or disabled API key"
        if user["scrapes_used"] >= user["scrapes_limit"]:
            return None, f"Scrape limit reached ({user['scrapes_limit']} scrapes). Contact support to upgrade."
        return dict(user), None

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-Admin-Token") or request.args.get("token")
        if not token:
            return jsonify({"error": "Unauthorized"}), 401
        with get_db() as db:
            session = db.execute("SELECT * FROM admin_sessions WHERE token=?", (token,)).fetchone()
        if not session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ─── SCRAPER CORE ─────────────────────────────────────────────────────────────

def scrape_google_maps(query: str, location: str, max_results: int = 20):
    """
    Scrapes Google Maps via their Places API or web endpoint.
    Uses SerpAPI if SERPAPI_KEY env var is set (recommended),
    otherwise falls back to a basic web scrape attempt.
    """
    results = []

    serpapi_key = os.environ.get("SERPAPI_KEY")

    if serpapi_key:
        # ── SerpAPI path (reliable, ~$50/mo for 5k searches) ──
        try:
            url = "https://serpapi.com/search"
            params = {
                "engine": "google_maps",
                "q": f"{query} {location}",
                "api_key": serpapi_key,
                "type": "search",
                "num": min(max_results, 20)
            }
            with httpx.Client(timeout=30) as client:
                r = client.get(url, params=params)
                data = r.json()

            for place in data.get("local_results", [])[:max_results]:
                results.append({
                    "name": place.get("title", ""),
                    "address": place.get("address", ""),
                    "phone": place.get("phone", ""),
                    "website": place.get("website", ""),
                    "rating": place.get("rating", ""),
                    "reviews": place.get("reviews", ""),
                    "category": place.get("type", ""),
                    "hours": place.get("hours", ""),
                    "maps_url": place.get("place_id_search", ""),
                })
        except Exception as e:
            results = [{"error": f"SerpAPI error: {str(e)}"}]

    else:
        # ── Fallback: Google Places Text Search (free $200 credit/mo) ──
        google_key = os.environ.get("GOOGLE_PLACES_KEY")
        if google_key:
            try:
                url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
                params = {
                    "query": f"{query} in {location}",
                    "key": google_key
                }
                with httpx.Client(timeout=30) as client:
                    r = client.get(url, params=params)
                    data = r.json()

                for place in data.get("results", [])[:max_results]:
                    # Get details (phone, website)
                    detail_url = "https://maps.googleapis.com/maps/api/place/details/json"
                    detail_params = {
                        "place_id": place["place_id"],
                        "fields": "name,formatted_phone_number,website,formatted_address,rating,user_ratings_total,types",
                        "key": google_key
                    }
                    dr = client.get(detail_url, params=detail_params).json()
                    d = dr.get("result", {})

                    results.append({
                        "name": d.get("name", place.get("name", "")),
                        "address": d.get("formatted_address", place.get("formatted_address", "")),
                        "phone": d.get("formatted_phone_number", ""),
                        "website": d.get("website", ""),
                        "rating": d.get("rating", place.get("rating", "")),
                        "reviews": d.get("user_ratings_total", ""),
                        "category": ", ".join(d.get("types", [])[:2]),
                        "hours": "",
                        "maps_url": f"https://www.google.com/maps/place/?q=place_id:{place['place_id']}"
                    })
            except Exception as e:
                results = [{"error": f"Google Places error: {str(e)}"}]
        else:
            # No API keys configured — return demo data so app still works
            results = generate_demo_results(query, location, max_results)

    return results


def generate_demo_results(query, location, count):
    """Demo data when no API keys are set. Good for testing."""
    demo = []
    businesses = [
        ("The Golden Spoon Restaurant", "+1 555-0101", "goldenspoon.com", "4.5", "Restaurant"),
        ("City Auto Repair", "+1 555-0102", "cityauto.com", "4.2", "Auto Repair"),
        ("Sunset Dental Clinic", "+1 555-0103", "sunsetdental.com", "4.8", "Dentist"),
        ("Metro Plumbing Co.", "+1 555-0104", "", "3.9", "Plumber"),
        ("Green Leaf Landscaping", "+1 555-0105", "greenleaf.com", "4.6", "Landscaping"),
        ("Peak Performance Gym", "+1 555-0106", "peakgym.com", "4.3", "Gym"),
        ("Bright Minds Tutoring", "+1 555-0107", "", "4.7", "Education"),
        ("Harbor View Hotel", "+1 555-0108", "harborview.com", "4.1", "Hotel"),
    ]
    for i, (name, phone, web, rating, cat) in enumerate(businesses[:count]):
        demo.append({
            "name": f"{name}",
            "address": f"{100+i*10} Main St, {location}",
            "phone": phone,
            "website": web,
            "rating": rating,
            "reviews": str(50 + i * 23),
            "category": cat,
            "hours": "Mon-Fri 9am-6pm",
            "maps_url": "",
            "note": "⚠ Demo data — add SERPAPI_KEY or GOOGLE_PLACES_KEY env var for real results"
        })
    return demo

# ─── PUBLIC API ENDPOINTS ─────────────────────────────────────────────────────

@app.route("/api/validate", methods=["POST"])
def validate():
    """Called by desktop app on startup to check if key works."""
    data = request.json or {}
    api_key = data.get("api_key", "")
    user, error = validate_key(api_key)
    if error:
        return jsonify({"valid": False, "error": error}), 401
    return jsonify({
        "valid": True,
        "name": user["name"],
        "plan": user["plan"],
        "scrapes_used": user["scrapes_used"],
        "scrapes_limit": user["scrapes_limit"],
        "scrapes_remaining": user["scrapes_limit"] - user["scrapes_used"]
    })

@app.route("/api/scrape", methods=["POST"])
def scrape():
    """Main scraping endpoint called by desktop app."""
    data = request.json or {}
    api_key = data.get("api_key", "")

    user, error = validate_key(api_key)
    if error:
        return jsonify({"success": False, "error": error}), 401

    query = data.get("query", "").strip()
    location = data.get("location", "").strip()
    max_results = min(int(data.get("max_results", 20)), 100)

    if not query or not location:
        return jsonify({"success": False, "error": "Query and location are required"}), 400

    # Run scraper
    results = scrape_google_maps(query, location, max_results)

    # Log usage
    with get_db() as db:
        db.execute(
            "UPDATE users SET scrapes_used = scrapes_used + 1 WHERE api_key=?", (api_key,)
        )
        db.execute(
            "INSERT INTO scrape_logs (api_key, query, location, results_count, ip) VALUES (?,?,?,?,?)",
            (api_key, query, location, len(results), request.remote_addr)
        )

    return jsonify({
        "success": True,
        "count": len(results),
        "results": results,
        "scrapes_remaining": user["scrapes_limit"] - user["scrapes_used"] - 1
    })

# ─── ADMIN AUTH ───────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.json or {}
    if data.get("password") != ADMIN_PASSWORD:
        return jsonify({"error": "Wrong password"}), 401
    token = secrets.token_urlsafe(32)
    with get_db() as db:
        # Clean old sessions
        db.execute("DELETE FROM admin_sessions WHERE created_at < datetime('now', '-7 days')")
        db.execute("INSERT INTO admin_sessions (token) VALUES (?)", (token,))
    return jsonify({"token": token})

# ─── ADMIN API ────────────────────────────────────────────────────────────────

@app.route("/admin/api/users", methods=["GET"])
@require_admin
def admin_users():
    with get_db() as db:
        users = db.execute(
            "SELECT id, name, email, api_key, active, plan, scrapes_used, scrapes_limit, created_at, notes FROM users ORDER BY created_at DESC"
        ).fetchall()
    return jsonify([dict(u) for u in users])

@app.route("/admin/api/users", methods=["POST"])
@require_admin
def admin_create_user():
    data = request.json or {}
    name = data.get("name", "").strip()
    email = data.get("email", "").strip()
    plan = data.get("plan", "basic")
    limit = int(data.get("scrapes_limit", 500))
    notes = data.get("notes", "")

    if not name or not email:
        return jsonify({"error": "Name and email required"}), 400

    key = generate_key()
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO users (name, email, api_key, plan, scrapes_limit, notes) VALUES (?,?,?,?,?,?)",
                (name, email, key, plan, limit, notes)
            )
        return jsonify({"success": True, "api_key": key})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already exists"}), 400

@app.route("/admin/api/users/<int:uid>", methods=["PATCH"])
@require_admin
def admin_update_user(uid):
    data = request.json or {}
    allowed = ["active", "plan", "scrapes_limit", "notes", "name"]
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with get_db() as db:
        db.execute(f"UPDATE users SET {set_clause} WHERE id=?", (*updates.values(), uid))
    return jsonify({"success": True})

@app.route("/admin/api/users/<int:uid>", methods=["DELETE"])
@require_admin
def admin_delete_user(uid):
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
    return jsonify({"success": True})

@app.route("/admin/api/users/<int:uid>/reset-key", methods=["POST"])
@require_admin
def admin_reset_key(uid):
    new_key = generate_key()
    with get_db() as db:
        db.execute("UPDATE users SET api_key=? WHERE id=?", (new_key, uid))
    return jsonify({"success": True, "api_key": new_key})

@app.route("/admin/api/stats", methods=["GET"])
@require_admin
def admin_stats():
    with get_db() as db:
        total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        active_users = db.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        total_scrapes = db.execute("SELECT SUM(scrapes_used) FROM users").fetchone()[0] or 0
        today_scrapes = db.execute(
            "SELECT COUNT(*) FROM scrape_logs WHERE timestamp >= date('now')"
        ).fetchone()[0]
        recent_logs = db.execute(
            """SELECT l.timestamp, u.name, l.query, l.location, l.results_count
               FROM scrape_logs l LEFT JOIN users u ON l.api_key = u.api_key
               ORDER BY l.timestamp DESC LIMIT 20"""
        ).fetchall()
    return jsonify({
        "total_users": total_users,
        "active_users": active_users,
        "total_scrapes": total_scrapes,
        "today_scrapes": today_scrapes,
        "recent_logs": [dict(r) for r in recent_logs]
    })

# ─── SERVE DASHBOARD ──────────────────────────────────────────────────────────

@app.route("/")
@app.route("/admin")
def serve_dashboard():
    return send_from_directory("static", "index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
