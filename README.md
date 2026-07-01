# Assistant Foyer

Petite app domestique : un tableau de bord mobile (température, liste de
courses) + un assistant qui répond en connaissant le contexte du foyer.

- **Cerveau** : API Mistral (distante) — rien à héberger côté modèle.
- **Interface** : ce projet (backend FastAPI + site web), hébergé sur le Pi.
- **Capteur** : Shelly H&T lu en HTTP (mode démo tant qu'il n'est pas branché).

```
Navigateur (tél/PC) ──▶ backend FastAPI (Pi) ──▶ API Mistral
                              │
                         SQLite + Shelly
```

## Lancer en local (pour tester sur le Mac)

```bash
cd ~/assistant-foyer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # puis ouvre .env et colle ta clé Mistral
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Ouvre ensuite **http://localhost:8000**.

- Sans clé Mistral → le tableau de bord marche, le chat affiche un rappel.
- Sans Shelly (`SHELLY_IP` vide) → température en **mode démo** (21 °C).

## Sur le Raspberry Pi

Mêmes commandes. Grâce à `--host 0.0.0.0`, l'app est accessible depuis ton
téléphone sur le WiFi à l'adresse **http://<ip-du-pi>:8000**.
Astuce iPhone/Android : « Ajouter à l'écran d'accueil » pour l'avoir comme une
vraie app plein écran.

## Brancher le vrai capteur

1. Configure le Shelly H&T sur ton WiFi, note son IP.
2. Mets cette IP dans `.env` → `SHELLY_IP=192.168.1.xx`.
3. Redémarre l'app : la température devient réelle (badge « démo » disparaît).

## Personnaliser l'assistant

Tout se joue dans **`profil_famille.md`** : qui vous êtes, préférences, ton,
limites. Il est injecté à chaque message. Édite-le librement, aucun redémarrage
nécessaire (relu à chaque requête).

## Structure

```
assistant-foyer/
├── profil_famille.md      # le contexte du foyer (system prompt)
├── backend/main.py        # API : widgets, courses, chat → Mistral
├── web/                   # le site (mobile-first)
│   ├── index.html
│   ├── style.css
│   └── app.js
├── requirements.txt
└── .env.example
```
