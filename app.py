import os
import json
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, render_template, g

app = Flask(__name__)

# ── DB接続（環境変数 DATABASE_URL があれば PostgreSQL、なければ SQLite）──────

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Render は "postgres://" で渡してくるが psycopg2 は "postgresql://" が必要
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2
    import psycopg2.extras
    PH = "%s"          # PostgreSQL のプレースホルダ
else:
    import sqlite3
    DB_PATH = os.path.join(os.path.dirname(__file__), "words.db")
    PH = "?"           # SQLite のプレースホルダ


def get_db():
    if "db" not in g:
        if USE_PG:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            g.db = conn
        else:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def db_execute(sql, params=()):
    """プレースホルダを環境に合わせて置換して実行。カーソルを返す。"""
    db = get_db()
    if USE_PG:
        cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    else:
        cur = db.cursor()
    cur.execute(sql.replace("?", PH), params)
    return cur


def db_commit():
    if not USE_PG:          # PG は後で手動コミット
        get_db().commit()
    else:
        get_db().commit()


def init_db():
    """直接接続でDBを初期化（Flask の g に依存しない）"""
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS words (
                id          SERIAL PRIMARY KEY,
                word        TEXT    NOT NULL UNIQUE,
                definition  TEXT    NOT NULL,
                examples    TEXT    NOT NULL,
                meanings    TEXT    NOT NULL DEFAULT '[]',
                is_idiom    BOOLEAN NOT NULL DEFAULT FALSE,
                phonetic    TEXT    NOT NULL DEFAULT '',
                audio_url   TEXT    NOT NULL DEFAULT '',
                difficulty  TEXT    DEFAULT 'new',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.close()
        conn.close()
    else:
        import sqlite3 as _sq
        with _sq.connect(DB_PATH) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS words (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    word        TEXT    NOT NULL UNIQUE,
                    definition  TEXT    NOT NULL,
                    examples    TEXT    NOT NULL,
                    meanings    TEXT    NOT NULL DEFAULT '[]',
                    is_idiom    INTEGER NOT NULL DEFAULT 0,
                    phonetic    TEXT    NOT NULL DEFAULT '',
                    audio_url   TEXT    NOT NULL DEFAULT '',
                    difficulty  TEXT    DEFAULT 'new',
                    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col, typedef in [
                ("meanings",  "TEXT NOT NULL DEFAULT '[]'"),
                ("is_idiom",  "INTEGER NOT NULL DEFAULT 0"),
                ("phonetic",  "TEXT NOT NULL DEFAULT ''"),
                ("audio_url", "TEXT NOT NULL DEFAULT ''"),
            ]:
                try:
                    con.execute(f"ALTER TABLE words ADD COLUMN {col} {typedef}")
                except Exception:
                    pass
            con.commit()


# ── Free Dictionary API ──────────────────────────────────────────────────────

def lookup_dictionary(phrase: str) -> dict:
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.request.quote(phrase)}"
    req = urllib.request.Request(url, headers={"User-Agent": "VocabFlash/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ValueError(f'"{phrase}" was not found in the dictionary.')
        raise

    phonetic = ""
    audio_url = ""
    for entry in data:
        if not phonetic and entry.get("phonetic"):
            phonetic = entry["phonetic"]
        for ph in entry.get("phonetics", []):
            if not phonetic and ph.get("text"):
                phonetic = ph["text"]
            if not audio_url and ph.get("audio"):
                audio_url = ph["audio"]
        if phonetic and audio_url:
            break

    pos_map, pos_order = {}, []
    for entry in data:
        for meaning in entry.get("meanings", []):
            pos = meaning.get("partOfSpeech", "other")
            if pos not in pos_map:
                pos_map[pos] = []
                pos_order.append(pos)
            for defn in meaning.get("definitions", []):
                text = defn.get("definition", "").strip()
                if not text:
                    continue
                example = defn.get("example", "").strip()
                if not any(d["definition"] == text for d in pos_map[pos]):
                    pos_map[pos].append({"definition": text, "example": example})

    if not pos_map:
        raise ValueError(f'No definition found for "{phrase}".')

    meanings  = [{"partOfSpeech": pos, "definitions": pos_map[pos]} for pos in pos_order]
    first_def = meanings[0]["definitions"][0]["definition"]

    examples = []
    for m in meanings:
        for d in m["definitions"]:
            if d["example"] and d["example"] not in examples:
                examples.append(d["example"])

    fallbacks = [
        f'She encountered "{phrase}" while reading an article.',
        f'The teacher explained the meaning of "{phrase}" to the class.',
        f'Using "{phrase}" correctly will improve your writing.',
    ]
    for fb in fallbacks:
        if len(examples) >= 3:
            break
        examples.append(fb)

    return {
        "definition": first_def,
        "examples":   examples[:3],
        "meanings":   meanings,
        "phonetic":   phonetic,
        "audio_url":  audio_url,
    }


# ── Row → dict ───────────────────────────────────────────────────────────────

def row_to_dict(r):
    # PostgreSQL は RealDictRow、SQLite は Row — どちらも [] でアクセス可能
    meanings_raw = r["meanings"]
    examples_raw = r["examples"]
    # PG は文字列、SQLite も文字列のはずだが念のため
    meanings = json.loads(meanings_raw) if isinstance(meanings_raw, str) else meanings_raw
    examples = json.loads(examples_raw) if isinstance(examples_raw, str) else examples_raw
    return {
        "id":         r["id"],
        "word":       r["word"],
        "definition": r["definition"],
        "examples":   examples,
        "meanings":   meanings,
        "is_idiom":   bool(r["is_idiom"]),
        "phonetic":   r["phonetic"]  or "",
        "audio_url":  r["audio_url"] or "",
        "difficulty": r["difficulty"],
        "created_at": str(r["created_at"]),
    }


SELECT_COLS = (
    "id, word, definition, examples, meanings, is_idiom, "
    "phonetic, audio_url, difficulty, created_at"
)

# 起動時にDB初期化
try:
    init_db()
except Exception as e:
    print(f"[WARNING] init_db failed: {e}")

# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/words", methods=["GET"])
def list_words():
    cur  = db_execute(f"SELECT {SELECT_COLS} FROM words ORDER BY created_at DESC")
    rows = cur.fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    data   = request.get_json()
    phrase = (data.get("word") or "").strip().lower()
    if not phrase:
        return jsonify({"error": "word is required"}), 400
    try:
        info = lookup_dictionary(phrase)
        return jsonify({"found": True, **info})
    except ValueError:
        return jsonify({"found": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/words", methods=["POST"])
def add_word():
    data     = request.get_json()
    word     = (data.get("word") or "").strip().lower()
    is_idiom = bool(data.get("is_idiom", False))

    if not word:
        return jsonify({"error": "word is required"}), 400

    cur = db_execute("SELECT id FROM words WHERE word = ?", (word,))
    if cur.fetchone():
        return jsonify({"error": f'"{word}" is already registered'}), 409

    if data.get("manual"):
        meanings   = data.get("meanings") or []
        definition = (data.get("definition") or "").strip()
        phonetic   = (data.get("phonetic")   or "").strip()
        audio_url  = (data.get("audio_url")  or "").strip()
        if not definition or not meanings:
            return jsonify({"error": "definition and meanings are required"}), 400
        examples = []
        for m in meanings:
            for d in m.get("definitions", []):
                ex = (d.get("example") or "").strip()
                if ex and ex not in examples:
                    examples.append(ex)
        info = {
            "definition": definition, "examples": examples,
            "meanings":   meanings,   "phonetic":  phonetic,
            "audio_url":  audio_url,
        }
    else:
        try:
            info = lookup_dictionary(word)
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            return jsonify({"error": f"Dictionary lookup failed: {str(e)}"}), 500

    db_execute(
        "INSERT INTO words (word, definition, examples, meanings, is_idiom, phonetic, audio_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (word, info["definition"], json.dumps(info["examples"]),
         json.dumps(info["meanings"]), is_idiom,
         info["phonetic"], info["audio_url"]),
    )
    db_commit()

    cur = db_execute(f"SELECT {SELECT_COLS} FROM words WHERE word=?", (word,))
    return jsonify(row_to_dict(cur.fetchone())), 201


@app.route("/api/words/<int:word_id>", methods=["PUT"])
def update_word(word_id):
    data       = request.get_json()
    word       = (data.get("word")       or "").strip().lower()
    definition = (data.get("definition") or "").strip()
    meanings   = data.get("meanings")
    is_idiom   = bool(data.get("is_idiom", False))
    phonetic   = (data.get("phonetic")  or "").strip()
    audio_url  = (data.get("audio_url") or "").strip()

    if not word:
        return jsonify({"error": "word is required"}), 400
    if not definition:
        return jsonify({"error": "definition is required"}), 400
    if not meanings:
        return jsonify({"error": "meanings is required"}), 400

    examples = []
    for m in meanings:
        for d in m.get("definitions", []):
            ex = (d.get("example") or "").strip()
            if ex and ex not in examples:
                examples.append(ex)

    cur = db_execute("SELECT id FROM words WHERE word=? AND id!=?", (word, word_id))
    if cur.fetchone():
        return jsonify({"error": f'"{word}" is already registered'}), 409

    db_execute(
        "UPDATE words SET word=?, definition=?, examples=?, meanings=?, "
        "is_idiom=?, phonetic=?, audio_url=? WHERE id=?",
        (word, definition, json.dumps(examples), json.dumps(meanings),
         is_idiom, phonetic, audio_url, word_id),
    )
    db_commit()

    cur = db_execute(f"SELECT {SELECT_COLS} FROM words WHERE id=?", (word_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_dict(row))


@app.route("/api/words/<int:word_id>/difficulty", methods=["PATCH"])
def update_difficulty(word_id):
    data = request.get_json()
    difficulty = data.get("difficulty")
    if difficulty not in ("new", "learning", "mastered"):
        return jsonify({"error": "invalid difficulty"}), 400
    db_execute("UPDATE words SET difficulty=? WHERE id=?", (difficulty, word_id))
    db_commit()
    return jsonify({"ok": True})


@app.route("/api/words/<int:word_id>", methods=["DELETE"])
def delete_word(word_id):
    db_execute("DELETE FROM words WHERE id=?", (word_id,))
    db_commit()
    return jsonify({"ok": True})


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
