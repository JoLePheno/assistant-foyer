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
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
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


init_db()


# --- lecture du capteur de température --------------------------------------
def read_temperature():
    """Lit le Shelly H&T (API locale Gen2/Plus). Renvoie une valeur de
    démonstration tant que le capteur n'est pas configuré ou injoignable."""
    if SHELLY_IP:
        try:
            s = requests.get(f"http://{SHELLY_IP}/rpc/Shelly.GetStatus", timeout=4).json()
            return {
                "temperature": round(s["temperature:0"]["tC"], 1),
                "humidity": round(s["humidity:0"]["rh"]),
                "source": "shelly",
            }
        except Exception:
            pass  # capteur injoignable → on bascule en démo
    return {"temperature": 21.0, "humidity": 48, "source": "demo"}


# --- API : données du foyer -------------------------------------------------
@app.get("/api/widgets")
def widgets():
    with db() as conn:
        rows = conn.execute("SELECT id, item, done FROM courses ORDER BY id").fetchall()
    return {"temperature": read_temperature(), "courses": [dict(r) for r in rows]}


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
