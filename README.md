# GameVault

A browser game vault disguised as a calculator.

Yes, it is sneaky.
Yes, it is multiplayer.
Yes, parts of it were absolutely **HOLY VIBE CODED** into existence.

## What this is

GameVault is a Flask-powered web app that:
- opens as a calculator on the front page
- unlocks the game portal with a secret calculator code
- serves a bunch of solo and multiplayer browser games
- keeps lightweight shared multiplayer state on the server
- works with plain HTTP requests instead of WebSockets

The current portal unlock code is:
- `1337` → games portal

## Main features

- Calculator-style front page disguise
- Solo games
- Room-based multiplayer games
- Shared player counts / leaderboard
- HTTP long-polling multiplayer transport
- Lightweight Flask backend with game state in memory

## Included games

### Solo
- 2048
- Snake
- Tetris
- Minesweeper
- Flapty
- Neon Run

### Multiplayer
- Drawing Party
- Word Bomb
- Chess
- Pong
- Battleship
- Trivia
- Bomberman

## Project structure

```text
gamevault/
├── app.py                    # Flask app + multiplayer state + game logic
├── requirements.txt          # Python dependencies
├── README.md                 # You are here
└── www/
    ├── index.html            # Calculator disguise / portal entry
    ├── games/                # Main game portal UI
    ├── x/                    # Legacy redirect helper
    ├── http-socket.js        # Browser WebSocket shim over HTTP
    ├── multiplayer-common.js # Shared multiplayer room/name handling
    ├── 2048/
    ├── snake/
    ├── tetris/
    ├── chess/
    ├── drawing/
    ├── wordgame/
    ├── pong/
    ├── battleship/
    ├── trivia/
    ├── bomberman/
    ├── minesweeper/
    ├── flapty/
    └── neonrun/
```

## How multiplayer works

This project used to rely on WebSockets, but it now uses HTTP endpoints instead.

Server endpoints:
- `POST /api/socket/open`
- `POST /api/socket/send`
- `GET /api/socket/poll`
- `POST /api/socket/close`

Browser side:
- `www/http-socket.js` provides a WebSocket-like shim
- the game pages still call `new WebSocket(...)`
- under the hood it uses HTTP long-polling

That means the game code can stay mostly simple while the transport stays tunnel/proxy-friendly.

## Running locally

### 1. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Start the app

```bash
python app.py
```

Then open:
- `http://localhost:5000`

## Running with gunicorn

```bash
gunicorn -w 1 --threads 8 --bind 127.0.0.1:5000 app:app
```

## Nginx example

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

## Notes on the codebase

A few honest notes:

- This project has some clean bits.
- This project also has some chaotic bits.
- Some of the UI is polished.
- Some of the logic was clearly assembled under the influence of pure momentum.
- In other words: **HOLY VIBE CODED**, but salvageable — and now much healthier.

## Current architecture notes

- Multiplayer rooms are namespaced internally by game
- Room codes are preserved in the actual URL
- Refresh should keep players in the same room
- Stale multiplayer clients are cleaned up server-side
- Player counts should no longer inflate on refresh

## Known legacy leftovers

- `www/x/` still exists as a legacy redirect helper, but the main multiplayer room flow now uses direct URLs
- The project still contains some older UI patterns that could be cleaned up further over time

## If you want to keep improving it

Good next steps:
- add proper persistent storage for leaderboard / rooms if desired
- add copy-room-code buttons to multiplayer pages
- tighten mobile UI layouts for each multiplayer game
- split `app.py` into separate modules once you get sick of scrolling through it

## License / ownership

This is your project. Go make it weirder.
