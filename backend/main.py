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
import sqlite3
from datetime import datetime, timezone
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
                   ts          TEXT NOT NULL
               )"""
        )


init_db()

# Au-delà de ce délai sans nouvelle mesure poussée, on considère la valeur
# périmée (le capteur n'a peut-être plus de batterie / plus de WiFi).
READING_MAX_AGE = 3 * 3600  # 3 heures


def save_reading(temperature, humidity, battery=None):
    with db() as conn:
        conn.execute(
            "INSERT INTO readings (temperature, humidity, battery, ts) VALUES (?, ?, ?, ?)",
            (temperature, humidity, battery, datetime.now(timezone.utc).isoformat()),
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


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
