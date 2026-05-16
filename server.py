"""
BOOSTA Bin Classification Server - AWS Edition
Flask backend - handles image uploads to S3, RDS PostgreSQL storage, and REST API
"""

import os
import json
import uuid
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

# AWS S3 Support
try:
    import boto3
    from botocore.exceptions import NoCredentialsError
    HAS_S3 = True
except ImportError:
    boto3 = None
    HAS_S3 = False

# PostgreSQL Support (Compatible with Amazon RDS)
PG_VERSION = None
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
    PG_VERSION = 2
except ImportError:
    try:
        import psycopg
        from psycopg.rows import dict_row
        HAS_POSTGRES = True
        PG_VERSION = 3
    except ImportError:
        psycopg2 = None
        HAS_POSTGRES = False
        print("  [Database Driver Warning] Neither psycopg2 nor psycopg (v3) found. Falling back to SQLite.")

app = Flask(__name__, static_folder="public", static_url_path="")

# ── Config ──────────────────────────────────────────────────────────────
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
DB_FILE = os.path.join(os.path.dirname(__file__), "boosta.db")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ── AWS S3 Config ──────────────────────────────────────────────────────
S3_BUCKET = os.environ.get("S3_BUCKET_NAME")
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

s3_client = None
if HAS_S3 and S3_BUCKET:
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
            region_name=AWS_REGION
        )
        print(f"  [Storage Engine] Amazon S3 ENABLED (Bucket: {S3_BUCKET}).")
    except Exception as e:
        print(f"  [Storage Error] S3 Client initialization failed: {e}")
else:
    print("  [Storage Engine] Local upload folder (EPHEMERAL without S3).")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# ── Database Helpers ──────────────────────────────────────────────────────
DB_URL = os.environ.get("DATABASE_URL", "").strip()
if DB_URL and DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

if DB_URL:
    print("  [Database Config] DATABASE_URL detected (RDS Compatible).")
else:
    print("  [Database Config] Warning: DATABASE_URL is MISSING. Using SQLite.")

if DB_URL and HAS_POSTGRES:
    print("  [Database Engine] PostgreSQL/RDS database ENABLED.")
else:
    print("  [Database Engine] Local SQLite database ENABLED.")

def execute_query(query, params=(), commit=False, fetchone=False, fetchall=False):
    is_postgres = bool(DB_URL and HAS_POSTGRES)
    
    if is_postgres:
        pg_query = query.replace('?', '%s')
        connect_kwargs = {}
        if "localhost" not in DB_URL and "127.0.0.1" not in DB_URL and "sslmode" not in DB_URL:
            connect_kwargs["sslmode"] = "require"
        
        if PG_VERSION == 3:
            conn = psycopg.connect(DB_URL, **connect_kwargs)
            cursor = conn.cursor(row_factory=dict_row)
        else:
            conn = psycopg2.connect(DB_URL, **connect_kwargs)
            cursor = conn.cursor(cursor_factory=RealDictCursor)
    else:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        pg_query = query

    try:
        cursor.execute(pg_query, params)
        if commit:
            conn.commit()
        
        if fetchone:
            row = cursor.fetchone()
            return dict(row) if row else None
        if fetchall:
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        return cursor.rowcount
    finally:
        cursor.close()
        conn.close()

def init_db():
    query = '''
        CREATE TABLE IF NOT EXISTS bins (
            id TEXT PRIMARY KEY,
            bin_id TEXT,
            aisle TEXT,
            reported_by TEXT,
            description TEXT,
            urgency INTEGER,
            boosta_categories TEXT,
            image_path TEXT,
            image_url TEXT,
            timestamp TEXT,
            status TEXT
        )
    '''
    execute_query(query, commit=True)
    
    try:
        execute_query("ALTER TABLE bins ADD COLUMN resolved_by TEXT", commit=True)
    except Exception: pass

    try:
        execute_query("ALTER TABLE bins ADD COLUMN resolved_at TEXT", commit=True)
    except Exception: pass

init_db()

# ── Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route("/api/bins", methods=["GET"])
def get_bins():
    urgency = request.args.get("urgency")
    category = request.args.get("category")

    query = "SELECT * FROM bins"
    params = []
    conditions = []
    if urgency:
        conditions.append("urgency = ?")
        params.append(int(urgency))
    if category:
        conditions.append("boosta_categories LIKE ?")
        params.append(f'%"{category}"%')
        
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
        
    query += " ORDER BY urgency DESC, timestamp DESC"
    rows = execute_query(query, params, fetchall=True)

    data = []
    for b in rows:
        try:
            b["boosta_categories"] = json.loads(b["boosta_categories"])
        except:
            b["boosta_categories"] = []
        data.append(b)

    return jsonify({"success": True, "count": len(data), "bins": data})

@app.route("/api/submit", methods=["POST"])
def submit_bin():
    bin_id = request.form.get("bin_id", "").strip()
    description = request.form.get("description", "").strip()
    urgency_raw = request.form.get("urgency", "1")
    boosta_raw = request.form.get("boosta_categories", "[]")
    aisle = request.form.get("aisle", "").strip()
    reported_by = request.form.get("reported_by", "").strip()

    try:
        urgency = int(urgency_raw)
        urgency = max(1, min(5, urgency))
    except ValueError: urgency = 1

    try:
        boosta_categories = json.loads(boosta_raw)
    except: boosta_categories = []

    image_path = None
    image_url = None
    if "image" in request.files:
        file = request.files["image"]
        if file and file.filename and allowed_file(file.filename):
            ext = file.filename.rsplit(".", 1)[1].lower()
            unique_name = f"{uuid.uuid4().hex}.{ext}"
            
            # Option 1: Amazon S3
            if s3_client and S3_BUCKET:
                try:
                    s3_client.upload_fileobj(
                        file,
                        S3_BUCKET,
                        unique_name,
                        ExtraArgs={'ContentType': f'image/{ext}'}
                    )
                    # Construct URL (assuming public access or configured CloudFront/etc)
                    image_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{unique_name}"
                    image_path = unique_name
                except Exception as e:
                    print(f"  [Error] S3 upload failed: {e}")
            
            # Option 2: Local Storage (Fallback)
            if not image_url:
                file.seek(0)
                save_path = os.path.join(UPLOAD_FOLDER, unique_name)
                file.save(save_path)
                image_path = unique_name
                image_url = f"/uploads/{unique_name}"

    record = {
        "id": str(uuid.uuid4()),
        "bin_id": bin_id,
        "aisle": aisle,
        "reported_by": reported_by,
        "description": description,
        "urgency": urgency,
        "boosta_categories": boosta_categories,
        "image_path": image_path,
        "image_url": image_url,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "open",
    }

    query = '''
        INSERT INTO bins (id, bin_id, aisle, reported_by, description, urgency, boosta_categories, image_path, image_url, timestamp, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    '''
    params = (
        record["id"], record["bin_id"], record["aisle"], record["reported_by"],
        record["description"], record["urgency"], json.dumps(record["boosta_categories"]),
        record["image_path"], record["image_url"], record["timestamp"], record["status"]
    )
    execute_query(query, params, commit=True)

    return jsonify({"success": True, "record": record}), 201

@app.route("/api/bins/<bin_record_id>", methods=["DELETE"])
def delete_bin(bin_record_id):
    row = execute_query("SELECT image_path FROM bins WHERE id = ?", (bin_record_id,), fetchone=True)
    if not row:
        return jsonify({"success": False, "error": "Record not found"}), 404

    img_path = row.get("image_path")
    if img_path:
        # Delete from S3
        if s3_client and S3_BUCKET:
            try:
                s3_client.delete_object(Bucket=S3_BUCKET, Key=img_path)
            except Exception as e:
                print(f"Error removing S3 object {img_path}: {e}")
        
        # Delete from Local
        local_file = os.path.join(UPLOAD_FOLDER, img_path)
        if os.path.exists(local_file):
            try: os.remove(local_file)
            except: pass

    execute_query("DELETE FROM bins WHERE id = ?", (bin_record_id,), commit=True)
    return jsonify({"success": True, "deleted": bin_record_id})

@app.route("/api/bins/<bin_record_id>/resolve", methods=["PATCH"])
def resolve_bin(bin_record_id):
    data = request.get_json(silent=True) or {}
    resolved_by = data.get("resolved_by", "").strip()

    row = execute_query("SELECT status FROM bins WHERE id = ?", (bin_record_id,), fetchone=True)
    if not row:
        return jsonify({"success": False, "error": "Record not found"}), 404

    new_status = "resolved" if row["status"] == "open" else "open"
    resolved_at = datetime.now(timezone.utc).isoformat() if new_status == "resolved" else None
    
    if new_status == "open": resolved_by = None
        
    execute_query("UPDATE bins SET status = ?, resolved_by = ?, resolved_at = ? WHERE id = ?", (new_status, resolved_by, resolved_at, bin_record_id), commit=True)
    
    record = execute_query("SELECT * FROM bins WHERE id = ?", (bin_record_id,), fetchone=True)
    try: record["boosta_categories"] = json.loads(record["boosta_categories"])
    except: record["boosta_categories"] = []

    return jsonify({"success": True, "record": record})

@app.route("/api/stats", methods=["GET"])
def get_stats():
    total_row = execute_query("SELECT COUNT(*) as count FROM bins", fetchone=True)
    total = total_row["count"] if total_row else 0
    
    open_row = execute_query("SELECT COUNT(*) as count FROM bins WHERE status = 'open'", fetchone=True)
    open_count = open_row["count"] if open_row else 0
    
    resolved_row = execute_query("SELECT COUNT(*) as count FROM bins WHERE status = 'resolved'", fetchone=True)
    resolved_count = resolved_row["count"] if resolved_row else 0
    
    urgency_rows = execute_query("SELECT urgency, COUNT(*) as count FROM bins GROUP BY urgency", fetchall=True)
    urgency_counts = {str(i): 0 for i in range(1, 6)}
    for r in urgency_rows:
        urgency_counts[str(r["urgency"])] = r["count"]
        
    category_counts = {"B": 0, "O1": 0, "O2": 0, "S": 0, "T": 0, "A": 0}
    boosta_rows = execute_query("SELECT boosta_categories FROM bins", fetchall=True)
    for r in boosta_rows:
        try:
            cats = json.loads(r["boosta_categories"])
            for cat in cats:
                if cat in category_counts: category_counts[cat] += 1
        except: pass

    return jsonify({
        "success": True,
        "total": total,
        "open": open_count,
        "resolved": resolved_count,
        "urgency_counts": urgency_counts,
        "category_counts": category_counts,
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  BOOSTA Bin Classification - AWS EDITION")
    print(f"  Running on port: {port}")
    print("=" * 55)
    
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        app.run(debug=True, port=port, host="0.0.0.0")
