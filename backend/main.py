"""Assistant Foyer — petit backend FastAPI.

Trois rôles :
  • sert le site web (le dossier ../web)
  • expose les données du foyer  (GET /api/widgets, /api/courses…)
  • route les prompts vers Mistral (POST /api/chat) en y injectant le profil
    famille + les données du moment.

Le LLM (le « cerveau ») n'est PAS hébergé ici : on appelle l'API Mistral
distante. Pour passer un jour en local, il suffira de changer l'URL ci-dessous.
"""

import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- chemins & config -------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
PROFILE_PATH = BASE_DIR / "profil_famille.md"
DB_PATH = BASE_DIR / "foyer.db"

load_dotenv(BASE_DIR / ".env")
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
SHELLY_IP = os.environ.get("SHELLY_IP", "")

# Température extérieure via Open-Meteo (gratuit, sans clé). Par défaut : Paris 16e.
OUTDOOR_LAT = os.environ.get("OUTDOOR_LAT", "48.8637")
OUTDOOR_LON = os.environ.get("OUTDOOR_LON", "2.2769")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

app = FastAPI(title="Assistant Foyer")


# --- base de données (SQLite) ----------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS courses (
                   id   INTEGER PRIMARY KEY AUTOINCREMENT,
                   item TEXT    NOT NULL,
                   done INTEGER  NOT NULL DEFAULT 0
               )"""
        )
        # Relevés poussés par le Shelly H&T (capteur sur batterie → il "pousse"
        # sa mesure à chaque réveil, on garde l'historique et on lit le dernier).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS readings (
                   id          INTEGER PRIMARY KEY AUTOINCREMENT,
                   temperature REAL,
                   humidity    REAL,
                   battery     INTEGER,
                   outdoor     REAL,
                   ts          TEXT NOT NULL
               )"""
        )
        # Migration : ajoute la colonne "outdoor" si la base existe déjà sans.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(readings)").fetchall()]
        if "outdoor" not in cols:
            conn.execute("ALTER TABLE readings ADD COLUMN outdoor REAL")
        # To-do list, séparée par personne (owner : "marion" ou "jonathan").
        conn.execute(
            """CREATE TABLE IF NOT EXISTS todos (
                   id    INTEGER PRIMARY KEY AUTOINCREMENT,
                   owner TEXT    NOT NULL,
                   item  TEXT    NOT NULL,
                   done  INTEGER NOT NULL DEFAULT 0
               )"""
        )


init_db()

# Au-delà de ce délai sans nouvelle mesure poussée, on considère la valeur
# périmée (le capteur n'a peut-être plus de batterie / plus de WiFi).
READING_MAX_AGE = 3 * 3600  # 3 heures


# Cache de la température extérieure pour ne pas appeler Open-Meteo trop souvent.
_outdoor_cache = {"ts": 0.0, "value": None}


def fetch_outdoor_temperature():
    """Température extérieure actuelle (Paris 16e par défaut) via Open-Meteo.

    Gratuit et sans clé API. Résultat mis en cache 5 min. Renvoie un float en
    °C, ou la dernière valeur connue (voire None) en cas d'échec réseau.
    """
    now = time.time()
    if _outdoor_cache["value"] is not None and now - _outdoor_cache["ts"] < 300:
        return _outdoor_cache["value"]
    try:
        r = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": OUTDOOR_LAT,
                "longitude": OUTDOOR_LON,
                "current": "temperature_2m",
            },
            timeout=5,
        )
        r.raise_for_status()
        value = float(r.json()["current"]["temperature_2m"])
        _outdoor_cache.update(ts=now, value=value)
        return value
    except Exception:
        return _outdoor_cache["value"]


def save_reading(temperature, humidity, battery=None):
    now = datetime.now(timezone.utc)
    with db() as conn:
        # Le Shelly envoie 2 requêtes par réveil (à ~1 s d'écart) : l'une avec la
        # température seule, l'autre avec température + humidité (via le rappel).
        # On fusionne ces doublons dans une même ligne pour un historique propre.
        row = conn.execute(
            "SELECT id, ts, temperature, humidity, outdoor FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            age = (now - datetime.fromisoformat(row["ts"])).total_seconds()
            if age < 90:
                new_t = temperature if temperature is not None else row["temperature"]
                new_h = humidity if humidity is not None else row["humidity"]
                # On réutilise la température extérieure déjà récupérée pour ce réveil.
                outdoor = row["outdoor"] if row["outdoor"] is not None else fetch_outdoor_temperature()
                conn.execute(
                    "UPDATE readings SET temperature = ?, humidity = ?, "
                    "battery = COALESCE(?, battery), outdoor = ?, ts = ? WHERE id = ?",
                    (new_t, new_h, battery, outdoor, now.isoformat(), row["id"]),
                )
                return
        # Nouveau réveil → on récupère la température extérieure du moment.
        outdoor = fetch_outdoor_temperature()
        conn.execute(
            "INSERT INTO readings (temperature, humidity, battery, outdoor, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (temperature, humidity, battery, outdoor, now.isoformat()),
        )


def latest_reading():
    """Dernière mesure poussée par le Shelly, avec son âge en secondes."""
    with db() as conn:
        row = conn.execute(
            "SELECT temperature, humidity, ts FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])).total_seconds()
    return {
        "temperature": row["temperature"],
        "humidity": row["humidity"],
        "ts": row["ts"],
        "age": age,
        "fresh": age <= READING_MAX_AGE,
    }


# --- lecture du capteur de température --------------------------------------
def read_temperature():
    """Renvoie la température ambiante, dans l'ordre de préférence :

    1. la dernière mesure **poussée** par le Shelly H&T (recommandé : un capteur
       sur batterie dort la plupart du temps, il envoie sa mesure à son réveil) ;
    2. une interrogation HTTP directe du Shelly (utile s'il est alimenté en
       continu, ex. Shelly Plus H&T en USB) ;
    3. une valeur de démonstration si rien n'est disponible.
    """
    # 1) mesure poussée récente
    last = latest_reading()
    if last and last["fresh"]:
        return {
            "temperature": round(last["temperature"], 1),
            "humidity": round(last["humidity"]) if last["humidity"] is not None else None,
            "source": "shelly",
            "updated": last["ts"],
        }

    # 2) interrogation directe (Shelly alimenté en continu)
    if SHELLY_IP:
        try:
            s = requests.get(f"http://{SHELLY_IP}/rpc/Shelly.GetStatus", timeout=4).json()
            return {
                "temperature": round(s["temperature:0"]["tC"], 1),
                "humidity": round(s["humidity:0"]["rh"]),
                "source": "shelly",
                "updated": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            pass  # capteur injoignable (probablement en veille) → on continue

    # 3) démo (ou dernière valeur connue même périmée, si elle existe)
    if last:
        return {
            "temperature": round(last["temperature"], 1),
            "humidity": round(last["humidity"]) if last["humidity"] is not None else None,
            "source": "stale",  # dernière valeur connue mais ancienne
            "updated": last["ts"],
        }
    return {"temperature": 21.0, "humidity": 48, "source": "demo"}


# --- API : données du foyer -------------------------------------------------
@app.get("/api/widgets")
def widgets():
    with db() as conn:
        rows = conn.execute("SELECT id, item, done FROM courses ORDER BY id").fetchall()
    return {"temperature": read_temperature(), "courses": [dict(r) for r in rows]}


@app.get("/api/history")
def history(hours: int = 24):
    """Historique des relevés de température/humidité sur les N dernières heures
    (par défaut 24 h). Alimente le graphique de la page /history."""
    hours = max(1, min(hours, 24 * 30))  # borne : 1 h à 30 jours
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT ts, temperature, humidity, outdoor FROM readings "
            "WHERE ts >= ? AND temperature IS NOT NULL ORDER BY ts",
            (since,),
        ).fetchall()
    return {
        "hours": hours,
        "points": [
            {
                "ts": r["ts"],
                "temperature": r["temperature"],
                "humidity": r["humidity"],
                "outdoor": r["outdoor"],
            }
            for r in rows
        ],
    }


def _to_float(v):
    """Convertit une valeur en float de façon tolérante.

    Le webhook Shelly peut envoyer la valeur entourée d'accolades (ex. "{26.3}")
    ou avec une unité : on extrait simplement le premier nombre présent.
    """
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        m = re.search(r"-?\d+(?:\.\d+)?", str(v))
        return float(m.group()) if m else None


def fetch_from_shelly(ip):
    """Interroge un Shelly (Gen1 ou Gen2) pour lire (temperature, humidity).

    Utile juste après qu'il nous a "pingés" : à cet instant il est réveillé,
    donc joignable même s'il est sur batterie. Renvoie (temp, hum) ou None.
    """
    if not ip:
        return None
    # Gen2 / Plus
    try:
        s = requests.get(f"http://{ip}/rpc/Shelly.GetStatus", timeout=3).json()
        t = s.get("temperature:0", {}).get("tC")
        h = s.get("humidity:0", {}).get("rh")
        if t is not None:
            return (t, h)
    except Exception:
        pass
    # Gen1 (H&T)
    try:
        s = requests.get(f"http://{ip}/status", timeout=3).json()
        t = s.get("tmp", {}).get("tC")
        h = s.get("hum", {}).get("value")
        if t is not None:
            return (t, h)
    except Exception:
        pass
    return None


@app.api_route("/api/report", methods=["GET", "POST"])
async def report(request: Request):
    """Point d'entrée pour les mesures **poussées** par le Shelly H&T.

    Configure côté Shelly (app Shelly → Settings → Actions / Webhook) une URL :
        http://<ip-du-pi>:8000/api/report
    Deux cas gérés :
      • l'URL contient déjà ?temp=..&hum=.. (Shelly Gen1, ou webhook avec
        placeholders) → on lit directement ces valeurs ;
      • l'URL est appelée sans valeur → le Shelly vient de se réveiller, on
        l'interroge en retour (via son IP) pour lire sa mesure.
    """
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params = {**params, **{k: str(v) for k, v in body.items()}}
        except Exception:
            pass  # pas de corps JSON → on se contente des query params

    temperature = _to_float(params.get("temp") or params.get("temperature"))
    humidity = _to_float(params.get("hum") or params.get("humidity"))
    battery = _to_float(params.get("bat") or params.get("battery"))

    # Aucune valeur fournie : on rappelle le Shelly pendant qu'il est réveillé.
    if temperature is None and humidity is None:
        fetched = fetch_from_shelly(request.client.host if request.client else None)
        if fetched:
            temperature, humidity = fetched

    if temperature is None and humidity is None:
        return {"ok": False, "error": "aucune valeur temp/hum reçue"}

    save_reading(temperature, humidity, int(battery) if battery is not None else None)
    return {"ok": True, "temperature": temperature, "humidity": humidity}


class CourseIn(BaseModel):
    item: str


@app.post("/api/courses")
def add_course(c: CourseIn):
    item = c.item.strip()
    with db() as conn:
        cur = conn.execute("INSERT INTO courses (item) VALUES (?)", (item,))
        return {"id": cur.lastrowid, "item": item, "done": 0}


@app.post("/api/courses/{course_id}/toggle")
def toggle_course(course_id: int):
    with db() as conn:
        conn.execute("UPDATE courses SET done = 1 - done WHERE id = ?", (course_id,))
    return {"ok": True}


@app.delete("/api/courses/{course_id}")
def delete_course(course_id: int):
    with db() as conn:
        conn.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    return {"ok": True}


# --- API : to-do list (par personne) ---------------------------------------
TODO_OWNERS = ("marion", "jonathan")


class TodoIn(BaseModel):
    owner: str
    item: str


@app.get("/api/todos")
def list_todos():
    """Renvoie les tâches groupées par personne + le nombre de tâches à faire."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, owner, item, done FROM todos ORDER BY done, id"
        ).fetchall()
    result = {owner: [] for owner in TODO_OWNERS}
    for r in rows:
        if r["owner"] in result:
            result[r["owner"]].append({"id": r["id"], "item": r["item"], "done": r["done"]})
    counts = {owner: sum(1 for t in items if not t["done"]) for owner, items in result.items()}
    return {"todos": result, "counts": counts}


@app.post("/api/todos")
def add_todo(t: TodoIn):
    owner = t.owner.strip().lower()
    item = t.item.strip()
    if owner not in TODO_OWNERS or not item:
        return {"ok": False, "error": "owner ou item invalide"}
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO todos (owner, item) VALUES (?, ?)", (owner, item)
        )
        return {"id": cur.lastrowid, "owner": owner, "item": item, "done": 0}


@app.post("/api/todos/{todo_id}/toggle")
def toggle_todo(todo_id: int):
    with db() as conn:
        conn.execute("UPDATE todos SET done = 1 - done WHERE id = ?", (todo_id,))
    return {"ok": True}


@app.delete("/api/todos/{todo_id}")
def delete_todo(todo_id: int):
    with db() as conn:
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    return {"ok": True}


# --- API : assistant (Mistral) ---------------------------------------------
class ChatIn(BaseModel):
    messages: list  # [{"role": "user"|"assistant", "content": "…"}]


@app.post("/api/chat")
def chat(body: ChatIn):
    if not MISTRAL_KEY:
        return {"reply": "⚠️ Clé Mistral absente : renseigne MISTRAL_API_KEY dans le fichier .env."}

    profil = PROFILE_PATH.read_text(encoding="utf-8") if PROFILE_PATH.exists() else ""
    temp = read_temperature()
    contexte = (
        f"Données du moment : il fait {temp['temperature']}°C "
        f"et {temp['humidity']}% d'humidité à la maison."
    )

    messages = [
        {"role": "system", "content": profil},     # contexte permanent (le foyer)
        {"role": "system", "content": contexte},    # contexte dynamique (l'instant)
        *body.messages,                              # l'historique de la conversation
    ]

    try:
        r = requests.post(
            MISTRAL_URL,
            headers={"Authorization": f"Bearer {MISTRAL_KEY}"},
            json={"model": MISTRAL_MODEL, "messages": messages},
            timeout=30,
        )
        r.raise_for_status()
        reply = r.json()["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        reply = f"⚠️ Erreur en contactant Mistral : {e}"
    return {"reply": reply}


# --- site web ---------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.get("/history")
@app.get("/historique")
def history_page():
    return FileResponse(WEB_DIR / "history.html")


@app.get("/todo")
@app.get("/todos")
def todo_page():
    return FileResponse(WEB_DIR / "todo.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
