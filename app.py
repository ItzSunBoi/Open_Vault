"""
GameVault — Flask HTTP multiplayer server
All games: Drawing Party, Word Bomb, Chess, Pong, Battleship, Trivia, Bomberman

Run:
    pip install -r requirements.txt
    python app.py

Behind nginx (recommended for SSL / Cloudflare Tunnel):
    gunicorn -w 1 --threads 8 --bind 127.0.0.1:5000 app:app
"""

import os, json, random, time, threading
from functools import lru_cache
from flask import Flask, send_from_directory, abort, request, jsonify, Response, redirect

try:
    from wordfreq import zipf_frequency
except Exception:
    zipf_frequency = None

try:
    import chess as pychess
except Exception:
    pychess = None

# ── App setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

BASE_DIR = os.path.dirname(__file__)
WWW = os.path.join(BASE_DIR, 'www')
DATA_DIR = os.path.join(BASE_DIR, 'data')
GAME_DATA_PATH = os.path.join(DATA_DIR, 'game_data.json')
LEADERBOARD_PATH = os.path.join(DATA_DIR, 'leaderboard.json')
PLAYER_PROFILE_COOKIE = 'gamevault_player'
PLAYER_PROFILE_MAX_AGE = 60 * 60 * 24 * 180  # 180 days


def create_client():
    client_id = os.urandom(16).hex()
    now = time.time()
    clients[client_id] = {
        'name': None, 'room': None, 'game': None,
        'queue': [], 'cond': threading.Condition(), 'alive': True,
        'created_at': now, 'last_seen': now, 'disconnected_at': None,
    }
    return client_id


def touch_client(client_id):
    info = clients.get(client_id)
    if info:
        info['last_seen'] = time.time()
        info['disconnected_at'] = None
        info['alive'] = True


def disconnect_client(client_id):
    info = clients.get(client_id)
    if not info:
        return
    info['alive'] = False
    info['disconnected_at'] = time.time()
    cond = info.get('cond')
    if cond is not None:
        with cond:
            cond.notify_all()
    broadcast_stats()


@app.post('/api/socket/open')
def api_socket_open():
    client_id = create_client()
    remembered = parse_player_profile_cookie(request.cookies.get(PLAYER_PROFILE_COOKIE))
    broadcast_stats()
    response = jsonify({'clientId': client_id, 'profile': remembered})
    if remembered:
        set_player_profile_cookie(response, remembered)
    return response


@app.post('/api/socket/send')
def api_socket_send():
    data = request.get_json(silent=True) or {}
    client_id = data.get('clientId')
    raw = data.get('message')
    if client_id not in clients:
        return jsonify({'ok': False, 'closed': True})
    touch_client(client_id)
    try:
        msg = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return jsonify({'error': 'invalid message'}), 400
    if not isinstance(msg, dict):
        return jsonify({'error': 'message must be an object'}), 400
    cookie_profile = parse_player_profile_cookie(request.cookies.get(PLAYER_PROFILE_COOKIE))
    updated_profile = handle_message(client_id, msg, cookie_profile)
    response = jsonify({'ok': True})
    if updated_profile:
        set_player_profile_cookie(response, updated_profile)
    return response


@app.get('/api/socket/poll')
def api_socket_poll():
    client_id = request.args.get('clientId', '')
    timeout = min(max(float(request.args.get('timeout', '25')), 0), 30)
    info = clients.get(client_id)
    if not info:
        return jsonify({'closed': True, 'messages': []})
    touch_client(client_id)
    cond = info['cond']
    deadline = time.time() + timeout
    with cond:
        while info.get('alive', True) and not info['queue']:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            cond.wait(timeout=remaining)
        if not info.get('alive', True):
            return jsonify({'closed': True})
        messages = info['queue'][:]
        info['queue'].clear()
    return Response(json.dumps({'messages': messages}), mimetype='application/json')


@app.post('/api/socket/close')
def api_socket_close():
    data = request.get_json(silent=True) or {}
    client_id = data.get('clientId')
    if client_id in clients:
        disconnect_client(client_id)
    return jsonify({'ok': True})


# ── Static page routes ────────────────────────────────────────────────────────
PAGES = [
    'games', 'x', '2048', 'snake', 'tetris', 'chess', 'drawing',
    'wordgame', 'pong', 'battleship', 'trivia', 'bomberman',
    'minesweeper', 'flapty', 'neonrun',
]

PAGE_ALIASES = {
    'vault': 'games',
    'vault-zero': 'x',
    'vault-grid': '2048',
    'vault-serpent': 'snake',
    'vault-stack': 'tetris',
    'vault-rook': 'chess',
    'vault-sketch': 'drawing',
    'vault-lex': 'wordgame',
    'vault-paddle': 'pong',
    'vault-fleet': 'battleship',
    'vault-quiz': 'trivia',
    'vault-blast': 'bomberman',
    'vault-mine': 'minesweeper',
    'vault-wing': 'flapty',
    'vault-neon': 'neonrun',
}

CANONICAL_PATHS = {value: key for key, value in PAGE_ALIASES.items()}

@app.route('/')
def index():
    return send_from_directory(WWW, 'index.html')

@app.route('/http-socket.js')
def http_socket_js():
    return send_from_directory(WWW, 'http-socket.js')

@app.route('/multiplayer-common.js')
def multiplayer_common_js():
    return send_from_directory(WWW, 'multiplayer-common.js')

@app.route('/favicon.ico')
def favicon():
    return ('', 204)

@app.route('/<page>/')
@app.route('/<page>')
def game_page(page):
    canonical_page = PAGE_ALIASES.get(page, page)
    if canonical_page in PAGES:
        if page == canonical_page and canonical_page in CANONICAL_PATHS:
            target = f"/{CANONICAL_PATHS[canonical_page]}/"
            if request.query_string:
                target += f"?{request.query_string.decode('utf-8', errors='ignore')}"
            return redirect(target, code=302)
        path = os.path.join(WWW, canonical_page, 'index.html')
        if os.path.exists(path):
            return send_from_directory(os.path.join(WWW, canonical_page), 'index.html')
    abort(404)

# ── Global state ──────────────────────────────────────────────────────────────
rooms      = {}          # roomId -> room dict
clients    = {}          # client_id -> info dict
leaderboard   = []
_glock = threading.Lock()   # guards rooms / clients / leaderboard / global_online

# ── External game data ────────────────────────────────────────────────────────
def load_game_data():
    with open(GAME_DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)
    return data

GAME_DATA = load_game_data()
DRAWING_WORDS = GAME_DATA.get('drawing_words', [])
WORD_BOMB_SYLLABLES = GAME_DATA.get('wordbomb_syllables', {})
TRIVIA_QUESTIONS = GAME_DATA.get('trivia_questions', [])

# ── Persistence / cookies ─────────────────────────────────────────────────────
def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def save_json_atomic(path, payload):
    ensure_data_dir()
    tmp_path = f'{path}.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def load_leaderboard():
    if not os.path.exists(LEADERBOARD_PATH):
        return []
    try:
        with open(LEADERBOARD_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        cleaned = []
        for row in data[:50]:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name', '')).strip()
            game = str(row.get('game', '')).strip()
            try:
                score = int(row.get('score', 0))
            except Exception:
                score = 0
            try:
                ts = int(row.get('time', int(time.time())))
            except Exception:
                ts = int(time.time())
            if name and game:
                cleaned.append({'name': name, 'game': game, 'score': score, 'time': ts})
        cleaned.sort(key=lambda x: (-x['score'], x['time']))
        return cleaned[:50]
    except Exception:
        return []


def persist_leaderboard():
    with _glock:
        snapshot = leaderboard[:50]
    save_json_atomic(LEADERBOARD_PATH, snapshot)


def parse_player_profile_cookie(raw):
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    profile = {
        'name': sanitize_player_name(data.get('name')),
        'room': sanitize_room_id(data.get('room')),
        'game': sanitize_game_name(data.get('game')),
    }
    return {k: v for k, v in profile.items() if v}


def set_player_profile_cookie(response, profile):
    payload = {
        'name': sanitize_player_name(profile.get('name')),
        'room': sanitize_room_id(profile.get('room')),
        'game': sanitize_game_name(profile.get('game')),
    }
    payload = {k: v for k, v in payload.items() if v}
    if not payload:
        return response
    response.set_cookie(
        PLAYER_PROFILE_COOKIE,
        json.dumps(payload, separators=(',', ':')),
        max_age=PLAYER_PROFILE_MAX_AGE,
        httponly=False,
        secure=request.is_secure,
        samesite='Lax',
        path='/',
    )
    return response

# ── Helpers ───────────────────────────────────────────────────────────────────
def ws_send(client_id, data):
    info = clients.get(client_id)
    if not info or not info.get('alive', True):
        return
    payload = json.dumps(data)
    cond = info.get('cond')
    if cond is None:
        return
    with cond:
        info['queue'].append(payload)
        cond.notify_all()


def broadcast(room, data, exclude=None):
    for client_id in list(room['clients']):
        if client_id != exclude:
            ws_send(client_id, data)

def broadcast_all(room, data):
    broadcast(room, data)

def rand_item(lst):
    return lst[random.randrange(len(lst))]

def shuffle(lst):
    lst = list(lst)
    random.shuffle(lst)
    return lst

def get_room_stats():
    stats = {g: 0 for g in ['drawing','wordgame','chess','pong','battleship','trivia','bomberman']}
    stats['total'] = sum(1 for info in clients.values() if info.get('alive'))
    for r in rooms.values():
        g = r['game']
        if g in stats:
            stats[g] += len([cid for cid in r['clients'] if clients.get(cid, {}).get('alive')])
    return stats

def broadcast_stats():
    data = {'type': 'stats', **get_room_stats()}
    for client_id in list(clients):
        ws_send(client_id, data)

def add_leaderboard(name, game, score):
    with _glock:
        leaderboard.append({'name': name, 'game': game, 'score': score, 'time': int(time.time())})
        leaderboard.sort(key=lambda x: (-x['score'], x['time']))
        del leaderboard[50:]
    persist_leaderboard()
    broadcast_leaderboard()

def broadcast_leaderboard():
    data = {'type': 'leaderboard', 'scores': leaderboard[:50]}
    for client_id in list(clients):
        ws_send(client_id, data)

def get_player_list(room):
    drawer = None
    if room.get('game') == 'drawing' and room.get('players') and room.get('game_running'):
        drawer = room['players'][room['drawer_index'] % len(room['players'])]['name']
    return [
        {'name': p['name'], 'score': p['score'], 'lives': p['lives'],
         'eliminated': p['eliminated'], 'active': False,
         'drawing': p['name'] == drawer}
        for p in room['players']
    ]

def cancel_timer(room, key):
    t = room.get(key)
    if t:
        try: t.cancel()
        except Exception: pass
        room[key] = None


def sanitize_player_name(name):
    if name is None:
        return None
    cleaned = ' '.join(str(name).strip().split())
    cleaned = cleaned[:24]
    return cleaned or None


def sanitize_room_id(room_id):
    if room_id is None:
        return None
    cleaned = ''.join(ch for ch in str(room_id).strip().upper() if ch.isalnum() or ch in ('-', '_'))
    return cleaned[:24] or None


def sanitize_game_name(game):
    if game is None:
        return None
    cleaned = str(game).strip().lower()
    return cleaned if cleaned in ['drawing','wordgame','chess','pong','battleship','trivia','bomberman'] else None


def find_name_conflict(name, exclude_client_id=None):
    wanted = (sanitize_player_name(name) or '').casefold()
    if not wanted:
        return False
    for cid, info in clients.items():
        if cid == exclude_client_id:
            continue
        if not info.get('alive'):
            continue
        other = (info.get('name') or '').casefold()
        if other == wanted:
            return True
    return False

def find_room_player(room, name):
    wanted = (sanitize_player_name(name) or '').casefold()
    if not wanted or not room:
        return None
    for player in room.get('players', []):
        if (player.get('name') or '').casefold() == wanted:
            return player
    return None


def choose_wordbomb_syllable(room):
    round_num = room.get('round', 0)
    if round_num < 4:
        difficulty = 'easy'
    elif round_num < 8:
        difficulty = 'medium'
    else:
        difficulty = 'hard'

    pool = list(WORD_BOMB_SYLLABLES.get(difficulty, []))
    if not pool:
        pool = list(WORD_BOMB_SYLLABLES.get('easy', [])) + list(WORD_BOMB_SYLLABLES.get('medium', [])) + list(WORD_BOMB_SYLLABLES.get('hard', []))
    if not pool:
        return ''

    cycle = room.setdefault('word_syllable_cycle', {})
    state = cycle.get(difficulty)
    if not state or state.get('pool') != pool or not state.get('remaining'):
        remaining = list(pool)
        random.shuffle(remaining)
        state = {'pool': list(pool), 'remaining': remaining}
        cycle[difficulty] = state

    syllable = state['remaining'].pop(0)
    if not state['remaining']:
        state['remaining'] = list(pool)
        random.shuffle(state['remaining'])
    return syllable


def sync_client_to_room(client_id, room, player):
    if not room or not player:
        return

    game = room.get('game')
    if game == 'wordgame' and room.get('game_running') and room.get('syllable'):
        player_list = get_player_list(room)
        current = room['players'][room['current_player_index']] if room.get('players') else None
        if current:
            for i, p in enumerate(player_list):
                p['active'] = room['players'][i]['name'] == current['name']
            ws_send(client_id, {
                'type': 'turn',
                'player': current['name'],
                'syllable': room['syllable'],
                'timeLeft': max(1, int(round(room.get('word_deadline', time.time()) - time.time()))),
                'players': player_list,
            })
    elif game == 'drawing' and room.get('game_running') and room.get('players'):
        drawer = room['players'][room['drawer_index'] % len(room['players'])]
        hint = ' '.join('_' for _ in (room.get('current_word') or ''))
        ws_send(client_id, {
            'type': 'round_start',
            'round': room.get('round', 1),
            'totalRounds': min(len(room['players']) * 2, 8),
            'drawer': drawer['name'],
            'word': room.get('current_word') if player['name'] == drawer['name'] else hint,
            'hint': hint,
            'timeLeft': max(1, int(round(room.get('round_deadline', time.time()) - time.time()))),
            'players': get_player_list(room),
            'drawingActions': room.get('drawing_actions', []),
        })
    elif game == 'trivia' and room.get('game_running') and room.get('trivia_questions') and room.get('trivia_round', 0) < len(room['trivia_questions']):
        q = room['trivia_questions'][room['trivia_round']]
        ws_send(client_id, {'type': 'trivia_start', 'total': len(room['trivia_questions'])})
        ws_send(client_id, {
            'type': 'trivia_question',
            'round': room['trivia_round'] + 1,
            'total': len(room['trivia_questions']),
            'question': q['q'],
            'options': room.get('trivia_current_options') or q['opts'],
            'timeLimit': max(1, int(round(room.get('trivia_deadline', time.time()) - time.time()))),
        })
    elif game == 'bomberman' and room.get('game_running'):
        ws_send(client_id, {'type': 'bomb_start', **get_bomb_state(room)})
    elif game == 'pong':
        ps = room.get('pong', {})
        if ps.get('running'):
            ws_send(client_id, {'type': 'pong_start', 'scores': ps.get('scores', {'left': 0, 'right': 0})})
            ws_send(client_id, {
                'type': 'pong_state',
                'ball': ps.get('ball', {'x': 400, 'y': 200}),
                'paddles': ps.get('paddles', {'left': 160, 'right': 160}),
            })
    elif game == 'chess':
        fen = room.get('chess_fen')
        if fen:
            ws_send(client_id, {'type': 'chess_sync', 'fen': fen, 'history': room.get('chess_history', [])})
            if len(room.get('players', [])) >= 2:
                ws_send(client_id, {'type': 'game_start', 'message': 'Game started!'})
    elif game == 'battleship':
        both_ready = len(room.get('players', [])) == 2 and all(p.get('bs_ready') for p in room.get('players', []))
        if both_ready:
            attacker_idx = room['players'].index(player)
            ws_send(client_id, {
                'type': 'bs_sync',
                'yourTurn': attacker_idx == room.get('bs_turn', 0),
                'selfGrid': player.get('bs_grid', []),
                'shots': room.get('bs_shots', {}).get(player['name'], []),
                'phase': 'battle',
            })
        elif player.get('bs_ready'):
            ws_send(client_id, {'type': 'bs_wait_opponent'})

@lru_cache(maxsize=20000)
def is_valid_wordbomb_word(word):
    w = (word or '').strip().lower()
    if not w or not w.isalpha() or len(w) < 3:
        return False
    if zipf_frequency is None:
        return False
    # Require the word to have at least some real usage in English.
    # This blocks obvious nonsense while keeping normal common words playable.
    return zipf_frequency(w, 'en') >= 1.5

# ── Room management ───────────────────────────────────────────────────────────
def create_room(room_key, game, public_room_id=None):
    return {
        'id': public_room_id or room_key, 'key': room_key, 'game': game, 'finalized': False,
        'clients': set(), 'players': [], 'host': None,
        'game_running': False, 'round': 0,
        'drawer_index': 0, 'current_word': None, 'drawing_actions': [],
        'round_timer': None, 'word_timer': None,
        'round_deadline': 0, 'word_deadline': 0,
        'current_player_index': 0, 'syllable': None,
        'word_syllable_cycle': {}, 'word_used_words': set(),
        # chess
        'chess_fen': None, 'chess_board': None, 'chess_history': [],
        # pong
        'pong': create_pong_state(),
        # battleship
        'bs_turn': 0, 'bs_shots': {},
        # trivia
        'trivia_questions': [], 'trivia_round': 0,
        'trivia_answers': {}, 'trivia_deadline': 0,
        'trivia_timer': None, 'trivia_current_options': [],
        # bomberman
        'bomb_map': [], 'bombs': [], 'flames': [], 'powerups': [],
        'bomb_next_id': 0, 'bomb_stop': None,
    }


def max_players_for_game(game):
    return {
        'pong': 2,
        'chess': 2,
        'battleship': 2,
        'bomberman': 4,
    }.get(game)


def join_room(client_id, room_id, name, game):
    info = clients.get(client_id, {})
    if info.get('room'):
        leave_room(client_id)

    safe_game = sanitize_game_name(game) or 'drawing'
    safe_room_id = sanitize_room_id(room_id) or 'DEFAULT'
    requested_name = sanitize_player_name(name)
    player_name = requested_name or f'Player{random.randint(100,999)}'

    room_key = f'{safe_game}:{safe_room_id}'
    with _glock:
        if room_key not in rooms:
            rooms[room_key] = create_room(room_key, safe_game, safe_room_id)
        room = rooms[room_key]
        existing_player = find_room_player(room, player_name)

    if room.get('finalized') and not existing_player:
        try:
            del rooms[room_key]
        except Exception:
            pass
        room = create_room(room_key, safe_game, safe_room_id)
        rooms[room_key] = room
        existing_player = None

    max_players = max_players_for_game(safe_game)
    if not existing_player and max_players and len(room.get('players', [])) >= max_players:
        ws_send(client_id, {
            'type': 'join_error',
            'error': 'This vault is full.'
        })
        return None

    if find_name_conflict(player_name, exclude_client_id=client_id):
        same_room_existing = existing_player and clients.get(existing_player.get('client_id'), {}).get('room') == room_key
        if not same_room_existing:
            ws_send(client_id, {
                'type': 'join_error',
                'error': 'That username is already in use. Please choose a different one.'
            })
            return None

    if room.get('finalized'):
        existing_player = None
    clients[client_id].update({'name': player_name, 'room': room_key, 'game': safe_game})

    reconnected = False
    player = existing_player
    if player:
        old_client_id = player.get('client_id')
        reconnected = True
        if old_client_id and old_client_id in clients and old_client_id != client_id:
            old_info = clients.get(old_client_id)
            if old_info:
                old_info['alive'] = False
                old_info.update({'name': None, 'room': None, 'game': None})
            room['clients'].discard(old_client_id)
            clients.pop(old_client_id, None)
        player['client_id'] = client_id
        room['clients'].add(client_id)
    else:
        player = {
            'name': player_name, 'client_id': client_id,
            'score': 0, 'lives': 3, 'eliminated': False,
            '_side': None, '_ready': False, '_chess_color': None,
            'bs_ships': [], 'bs_grid': [], 'bs_ready': False, 'bs_sunk': 0,
            'trivia_answered': False,
            'bomb_pos': {'r': 0, 'c': 0}, 'bomb_power': 2,
            'bomb_max': 1, 'bomb_count': 0, 'alive': True, 'color': None,
            'guessed': False,
        }
        room['clients'].add(client_id)
        room['players'].append(player)

    player_list = get_player_list(room)

    if reconnected:
        if safe_game == 'pong':
            ws_send(client_id, {'type': 'joined', 'side': player.get('_side'), 'players': player_list})
        elif safe_game == 'chess':
            ws_send(client_id, {'type': 'joined', 'color': player.get('_chess_color'), 'players': player_list})
        elif safe_game == 'battleship':
            ws_send(client_id, {'type': 'joined', 'players': player_list, 'isHost': room.get('host') == client_id})
        else:
            ws_send(client_id, {'type': 'joined', 'players': player_list, 'isHost': room.get('host') == client_id})
        sync_client_to_room(client_id, room, player)
        broadcast_all(room, {'type': 'player_reconnected', 'name': player_name, 'players': player_list})
        return {'name': player_name, 'room': safe_room_id, 'game': safe_game}

    if safe_game == 'pong':
        side = 'left' if len(room['players']) <= 1 else 'right'
        player['_side'] = side
        ws_send(client_id, {'type': 'joined', 'side': side, 'players': player_list})
        broadcast(room, {'type': 'player_joined', 'name': player_name, 'players': player_list}, client_id)
        if len(room['players']) == 2:
            broadcast_all(room, {'type': 'pong_waiting', 'message': 'Both players connected! Press Ready.'})
        else:
            ws_send(client_id, {'type': 'pong_waiting', 'message': 'Waiting for opponent...'})

    elif safe_game == 'chess':
        if len(room['players']) == 1:
            color = 'w'
        elif len(room['players']) == 2:
            color = 'b' if room['players'][0].get('_chess_color') == 'w' else 'w'
        else:
            color = 'w'
        player['_chess_color'] = color
        if pychess is not None and room.get('chess_board') is None:
            room['chess_board'] = pychess.Board()
            room['chess_fen'] = room['chess_board'].fen()
        if len(room['players']) == 2:
            colors = ['w', 'b']
            random.shuffle(colors)
            room['players'][0]['_chess_color'] = colors[0]
            room['players'][1]['_chess_color'] = colors[1]
            player_list = get_player_list(room)
            if pychess is not None:
                room['chess_board'] = pychess.Board()
                room['chess_fen'] = room['chess_board'].fen()
                room['chess_history'] = []
            room['finalized'] = False
            for cp in room['players']:
                ws_send(cp['client_id'], {'type': 'joined', 'color': cp.get('_chess_color'), 'players': player_list})
            broadcast_all(room, {'type': 'game_start', 'message': 'Game started!'})
            if room.get('chess_fen'):
                for cp in room['players']:
                    ws_send(cp['client_id'], {'type': 'chess_sync', 'fen': room['chess_fen'], 'history': room.get('chess_history', [])})
        else:
            ws_send(client_id, {'type': 'joined', 'color': color, 'players': player_list})

    elif safe_game == 'battleship':
        is_host = len(room['players']) == 1
        if is_host:
            room['host'] = client_id
        ws_send(client_id, {'type': 'joined', 'players': player_list, 'isHost': is_host})
        broadcast(room, {'type': 'player_joined', 'name': player_name, 'players': player_list}, client_id)
        if len(room['players']) == 2:
            broadcast_all(room, {'type': 'bs_start_placing'})

    else:
        is_host = len(room['players']) == 1
        if is_host:
            room['host'] = client_id
        ws_send(client_id, {'type': 'joined', 'players': player_list, 'isHost': is_host})
        broadcast(room, {'type': 'player_joined', 'name': player_name, 'players': player_list, 'isHost': is_host}, client_id)
        if room['game_running']:
            ws_send(client_id, {'type': 'waiting', 'message': 'Game in progress, wait for next round.', 'canStart': False})
        else:
            broadcast_all(room, {
                'type': 'waiting',
                'message': f"{len(room['players'])} player(s) in room. Need 2 to start.",
                'canStart': is_host or len(room['players']) >= 2
            })

    return {'name': player_name, 'room': safe_room_id, 'game': safe_game}


def leave_room(client_id):
    info = clients.get(client_id, {})
    room_id = info.get('room')
    if not room_id:
        return
    with _glock:
        room = rooms.get(room_id)
        if not room:
            return
        removed_index = next((i for i, p in enumerate(room['players']) if p['client_id'] == client_id), None)
        room['clients'].discard(client_id)
        room['players'] = [p for p in room['players'] if p['client_id'] != client_id]
        if room['host'] == client_id and room['players']:
            room['host'] = room['players'][0]['client_id']
        if removed_index is not None:
            if room.get('game') == 'drawing' and room.get('players'):
                if removed_index < room.get('drawer_index', 0):
                    room['drawer_index'] = max(0, room.get('drawer_index', 0) - 1)
                elif room.get('drawer_index', 0) >= len(room['players']):
                    room['drawer_index'] = 0
            if room.get('game') == 'wordgame' and room.get('players'):
                if removed_index <= room.get('current_player_index', 0):
                    room['current_player_index'] = max(-1, room.get('current_player_index', 0) - 1)
                if room.get('current_player_index', 0) >= len(room['players']):
                    room['current_player_index'] = -1
            if room.get('game') == 'battleship' and len(room['players']) < 2:
                room['bs_turn'] = 0

    player_list = get_player_list(room)
    broadcast_all(room, {'type': 'player_left', 'name': info.get('name'), 'players': player_list})

    if room['game'] == 'wordgame' and room.get('game_running') and len(room['players']) <= 1:
        cancel_timer(room, 'word_timer')
        end_word_game(room)
    elif room['game'] == 'drawing' and room.get('game_running') and len(room['players']) <= 1:
        cancel_timer(room, 'round_timer')
        end_drawing_game(room)
    elif room['game'] == 'trivia' and room.get('game_running') and len(room['players']) <= 1:
        cancel_timer(room, 'trivia_timer')
        end_trivia(room)
    elif room['game'] == 'bomberman' and room.get('game_running') and len(room['players']) <= 1:
        end_bomberman(room, room['players'][0] if room['players'] else None)
    elif room['game'] == 'pong' and room.get('pong', {}).get('running') and len(room['players']) <= 1:
        room['pong']['running'] = False
        stop_pong = room.get('pong', {}).get('stop')
        if stop_pong:
            stop_pong.set()
        broadcast_all(room, {'type': 'opponent_left'})

    with _glock:
        if not room['players']:
            # Stop all background threads/timers
            cancel_timer(room, 'round_timer')
            cancel_timer(room, 'word_timer')
            cancel_timer(room, 'trivia_timer')
            stop_ev = room.get('bomb_stop')
            if stop_ev:
                stop_ev.set()
            pong = room.get('pong', {})
            stop_pong = pong.get('stop')
            if stop_pong:
                stop_pong.set()
            del rooms[room_id]
        elif room['game'] == 'chess':
            broadcast_all(room, {'type': 'opponent_left'})

    if client_id in clients:
        clients[client_id].update({'name': None, 'room': None, 'game': None})

# ── HTTP socket message handling ──────────────────────────────────────────────
def handle_message(client_id, msg, cookie_profile=None):
    t = msg.get('type')

    if t == 'get_stats':
        ws_send(client_id, {'type': 'stats', **get_room_stats()})
        ws_send(client_id, {'type': 'leaderboard', 'scores': leaderboard[:50]})
        return None

    if t == 'join':
        joined = join_room(client_id, msg.get('room', 'default'), msg.get('name'), msg.get('game', 'drawing'))
        broadcast_stats()
        if joined:
            return joined
        return cookie_profile

    info = clients.get(client_id, {})
    room_id = info.get('room')
    if not room_id:
        return cookie_profile
    room = rooms.get(room_id)
    if not room:
        return cookie_profile

    game = room['game']
    if   game == 'drawing':    handle_drawing_msg(client_id, room, msg)
    elif game == 'wordgame':   handle_word_msg(client_id, room, msg)
    elif game == 'chess':      handle_chess_msg(client_id, room, msg)
    elif game == 'pong':       handle_pong_msg(client_id, room, msg)
    elif game == 'battleship': handle_battleship_msg(client_id, room, msg)
    elif game == 'trivia':     handle_trivia_msg(client_id, room, msg)
    elif game == 'bomberman':  handle_bomberman_msg(client_id, room, msg)

    if t == 'start_game' and room['host'] == client_id:
        if   game == 'drawing':   start_drawing_game(room)
        elif game == 'wordgame':  start_word_game(room)
        elif game == 'trivia':    start_trivia(room)
        elif game == 'bomberman': start_bomberman(room)

    return cookie_profile or {
        'name': info.get('name'),
        'room': room.get('id'),
        'game': room.get('game'),
    }

# ── Drawing ───────────────────────────────────────────────────────────────────
def start_drawing_game(room):
    room['finalized'] = False
    if room.get('game_running'):
        return
    if len(room['players']) < 2:
        broadcast_all(room, {'type': 'waiting', 'message': 'Need at least 2 players!', 'canStart': True})
        return
    room['game_running'] = True
    room['round'] = 0
    for p in room['players']:
        p['score'] = 0
        p['guessed'] = False
    room['drawer_index'] = 0
    next_drawing_round(room)

def next_drawing_round(room):
    max_rounds = min(len(room['players']) * 2, 8)
    if room['round'] >= max_rounds:
        end_drawing_game(room)
        return
    room['round'] += 1
    room['current_word'] = rand_item(DRAWING_WORDS)
    room['drawing_actions'] = []
    room['round_deadline'] = time.time() + 60
    for p in room['players']:
        p['guessed'] = False
    drawer = room['players'][room['drawer_index'] % len(room['players'])]
    hint = ' '.join('_' for _ in room['current_word'])
    total = min(len(room['players']) * 2, 8)

    for p in room['players']:
        is_drawer = p['name'] == drawer['name']
        ws_send(p['client_id'], {
            'type': 'round_start', 'round': room['round'], 'totalRounds': total,
            'drawer': drawer['name'], 'word': room['current_word'] if is_drawer else hint,
            'hint': hint, 'timeLeft': 60, 'players': get_player_list(room)
        })

    time_left = [60]
    def tick():
        if not room.get('game_running'):
            return
        time_left[0] -= 1
        broadcast_all(room, {'type': 'timer', 'value': time_left[0]})
        if time_left[0] <= 0:
            end_drawing_round(room)
        else:
            room['round_timer'] = threading.Timer(1.0, tick)
            room['round_timer'].daemon = True
            room['round_timer'].start()

    cancel_timer(room, 'round_timer')
    room['round_timer'] = threading.Timer(1.0, tick)
    room['round_timer'].daemon = True
    room['round_timer'].start()

def end_drawing_round(room):
    cancel_timer(room, 'round_timer')
    broadcast_all(room, {'type': 'round_end', 'word': room['current_word'], 'players': get_player_list(room)})
    room['drawer_index'] += 1
    t = threading.Timer(3.0, next_drawing_round, args=[room])
    t.daemon = True
    t.start()

def end_drawing_game(room):
    room['game_running'] = False
    room['finalized'] = True
    room['trivia_deadline'] = 0
    sorted_p = sorted(room['players'], key=lambda p: -p['score'])
    winner = sorted_p[0] if sorted_p else None
    broadcast_all(room, {'type': 'game_over', 'winner': winner['name'] if winner else '?', 'players': get_player_list(room)})
    if winner:
        add_leaderboard(winner['name'], 'Drawing', winner['score'])

def handle_drawing_msg(client_id, room, msg):
    t = msg.get('type')
    info = clients.get(client_id, {})
    drawer_idx = room['drawer_index'] % len(room['players']) if room['players'] else 0
    drawer = room['players'][drawer_idx] if room['players'] else None

    if t == 'draw' and drawer and info.get('name') == drawer['name']:
        action_payload = {'type': 'draw', 'action': msg.get('action'), 'x': msg.get('x'),
                         'y': msg.get('y'), 'color': msg.get('color'), 'size': msg.get('size')}
        room.setdefault('drawing_actions', []).append(action_payload)
        broadcast(room, action_payload, client_id)
    if t == 'draw_clear' and drawer and info.get('name') == drawer['name']:
        room['drawing_actions'] = [{'type': 'draw_clear'}]
        broadcast(room, {'type': 'draw_clear'}, client_id)
    if t == 'guess':
        word = (msg.get('word') or '').lower().strip()
        player = next((p for p in room['players'] if p['name'] == info.get('name')), None)
        if not player or player['guessed'] or (drawer and player['name'] == drawer['name']):
            return
        correct = room['game_running'] and word == (room['current_word'] or '').lower()
        if correct:
            player['guessed'] = True
            guessed_count = sum(1 for p in room['players'] if p['guessed'])
            points = int(100 * (1 - (1 - guessed_count / len(room['players'])) * 0.5))
            player['score'] += points
            if drawer:
                drawer['score'] += 20
        broadcast_all(room, {'type': 'guess_msg', 'name': info.get('name'), 'word': msg.get('word'), 'correct': correct})
        if correct:
            cancel_timer(room, 'round_timer')
            t2 = threading.Timer(1.0, end_drawing_round, args=[room])
            t2.daemon = True
            t2.start()

# ── Word Bomb ─────────────────────────────────────────────────────────────────
def start_word_game(room):
    room['finalized'] = False
    if room.get('game_running'):
        return
    if len(room['players']) < 2:
        broadcast_all(room, {'type': 'waiting', 'message': 'Need at least 2 players!', 'canStart': True})
        return
    room['game_running'] = True
    for p in room['players']:
        p['score'] = 0
        p['lives'] = 3
        p['eliminated'] = False
    room['round'] = 0
    room['current_player_index'] = -1
    room['word_used_words'] = set()
    broadcast_all(room, {'type': 'game_start'})
    next_word_turn(room)

def next_word_turn(room):
    alive = [p for p in room['players'] if not p['eliminated']]
    if len(alive) <= 1:
        end_word_game(room)
        return
    tries = 0
    while True:
        room['current_player_index'] = (room['current_player_index'] + 1) % len(room['players'])
        tries += 1
        if not room['players'][room['current_player_index']]['eliminated'] or tries >= len(room['players']):
            break
    player = room['players'][room['current_player_index']]
    round_num = room.get('round', 0)
    room['syllable'] = choose_wordbomb_syllable(room)
    time_limit = max(5, 12 - round_num // max(len(alive), 1))
    player_list = get_player_list(room)
    for i, p in enumerate(player_list):
        p['active'] = room['players'][i]['name'] == player['name']
    broadcast_all(room, {'type': 'turn', 'player': player['name'], 'syllable': room['syllable'],
                         'timeLeft': time_limit, 'players': player_list})
    cancel_timer(room, 'word_timer')
    room['word_deadline'] = time.time() + time_limit
    room['word_timer'] = threading.Timer(time_limit, handle_word_timeout, args=[room, player])
    room['word_timer'].daemon = True
    room['word_timer'].start()

def handle_word_timeout(room, player):
    player['lives'] -= 1
    failed = player['lives'] <= 0
    if failed:
        player['eliminated'] = True
    broadcast_all(room, {'type': 'word_result', 'player': player['name'], 'word': None,
                         'valid': False, 'players': get_player_list(room)})
    if failed:
        broadcast_all(room, {'type': 'player_eliminated', 'player': player['name']})
    t = threading.Timer(1.2, next_word_turn, args=[room])
    t.daemon = True
    t.start()

def end_word_game(room):
    room['game_running'] = False
    room['finalized'] = True
    room['word_deadline'] = 0
    alive = [p for p in room['players'] if not p['eliminated']]
    winner = alive[0] if alive else sorted(room['players'], key=lambda p: -p['score'])[0] if room['players'] else None
    broadcast_all(room, {'type': 'game_over', 'winner': winner['name'] if winner else '?', 'players': get_player_list(room)})
    if winner:
        add_leaderboard(winner['name'], 'Word Bomb', winner['score'])

def handle_word_msg(client_id, room, msg):
    if msg.get('type') != 'word':
        return
    info = clients.get(client_id, {})
    current = room['players'][room['current_player_index']] if room['players'] else None
    if not current or current['name'] != info.get('name'):
        return
    raw_word = (msg.get('word') or '').strip()
    word = raw_word.upper()
    normalized_word = ''.join(ch for ch in raw_word.lower() if ch.isalpha())
    syllable = room.get('syllable', '')
    used_words = room.setdefault('word_used_words', set())
    valid = (syllable in word and len(word) >= 3 and word.isalpha() and
             normalized_word not in used_words and normalized_word != syllable.lower() and
             is_valid_wordbomb_word(raw_word))
    cancel_timer(room, 'word_timer')
    if valid:
        used_words.add(normalized_word)
        points = len(word) * 5
        current['score'] += points
        broadcast_all(room, {'type': 'word_result', 'player': info.get('name'), 'word': msg.get('word'),
                             'valid': True, 'points': points, 'players': get_player_list(room)})
        room['round'] = room.get('round', 0) + 1
        t = threading.Timer(0.8, next_word_turn, args=[room])
        t.daemon = True
        t.start()
    else:
        current['lives'] -= 1
        eliminated = current['lives'] <= 0
        if eliminated:
            current['eliminated'] = True
        broadcast_all(room, {'type': 'word_result', 'player': info.get('name'), 'word': msg.get('word'),
                             'valid': False, 'players': get_player_list(room)})
        if eliminated:
            broadcast_all(room, {'type': 'player_eliminated', 'player': info.get('name')})
        alive = [p for p in room['players'] if not p['eliminated']]
        delay = 1.0
        if len(alive) <= 1:
            t = threading.Timer(delay, end_word_game, args=[room])
        else:
            t = threading.Timer(delay, next_word_turn, args=[room])
        t.daemon = True
        t.start()

# ── Chess ─────────────────────────────────────────────────────────────────────
def handle_chess_msg(client_id, room, msg):
    if msg.get('type') != 'chess_move':
        return
    if len(room.get('players', [])) < 2:
        ws_send(client_id, {'type': 'error', 'message': 'Waiting for the other player to join.', 'fen': room.get('chess_fen')})
        return
    player = next((p for p in room.get('players', []) if p.get('client_id') == client_id), None)
    if not player:
        return
    if pychess is None:
        return

    board = room.get('chess_board')
    if board is None:
        try:
            board = pychess.Board(room.get('chess_fen')) if room.get('chess_fen') else pychess.Board()
        except Exception:
            board = pychess.Board()
        room['chess_board'] = board
        room['chess_fen'] = board.fen()

    expected_color = player.get('_chess_color')
    if expected_color not in ('w', 'b') or board.turn != (expected_color == 'w'):
        ws_send(client_id, {'type': 'error', 'message': 'Not your turn.', 'fen': room.get('chess_fen')})
        return

    from_sq = str(msg.get('from') or '').strip()
    to_sq = str(msg.get('to') or '').strip()
    promotion = str(msg.get('promotion') or '').strip().lower()

    candidate_uci = [f"{from_sq}{to_sq}"]
    if promotion in ('q', 'r', 'b', 'n'):
        candidate_uci.insert(0, f"{from_sq}{to_sq}{promotion}")

    move = None
    for uci in candidate_uci:
        try:
            candidate = pychess.Move.from_uci(uci)
        except Exception:
            continue
        if candidate in board.legal_moves:
            move = candidate
            break

    if move is None:
        ws_send(client_id, {'type': 'error', 'message': 'Illegal move.', 'fen': room.get('chess_fen')})
        return

    san = board.san(move)
    board.push(move)
    room['chess_fen'] = board.fen()
    room.setdefault('chess_history', []).append(san)
    ws_send(client_id, {'type': 'chess_sync', 'fen': room['chess_fen'], 'history': room.get('chess_history', [])})
    broadcast(room, {
        'type': 'chess_move',
        'from': from_sq,
        'to': to_sq,
        'promotion': promotion if promotion in ('q','r','b','n') else None,
        'fen': room['chess_fen'],
        'history': room.get('chess_history', []),
    }, client_id)

# ── Pong ──────────────────────────────────────────────────────────────────────
PONG_W, PONG_H = 800, 400
PONG_PAD_H, PONG_PAD_W = 80, 12
PONG_BALL_R = 8
PONG_WIN = 7

def create_pong_state():
    return {
        'ball': {'x': 400.0, 'y': 200.0, 'vx': 4.0, 'vy': 3.0},
        'paddles': {'left': 160.0, 'right': 160.0},
        'scores': {'left': 0, 'right': 0},
        'running': False,
        'stop': None,
    }

def reset_pong_ball(ps, direction):
    ps['ball'] = {
        'x': float(PONG_W / 2), 'y': float(PONG_H / 2),
        'vx': (3.5 + random.random()) * direction,
        'vy': random.uniform(-2, 2),
    }

def start_pong_loop(room):
    ps = room['pong']
    ps['running'] = True
    stop_ev = threading.Event()
    ps['stop'] = stop_ev

    def loop():
        target = time.monotonic()
        while not stop_ev.is_set() and ps['running']:
            tick_pong(room)
            target += 1/60
            sleep_t = target - time.monotonic()
            if sleep_t > 0:
                time.sleep(sleep_t)

    t = threading.Thread(target=loop, daemon=True)
    t.start()

def tick_pong(room):
    ps = room['pong']
    if not ps['running']:
        return
    b = ps['ball']
    b['x'] += b['vx']
    b['y'] += b['vy']

    # Walls
    if b['y'] - PONG_BALL_R <= 0:
        b['y'] = float(PONG_BALL_R)
        b['vy'] = abs(b['vy'])
    if b['y'] + PONG_BALL_R >= PONG_H:
        b['y'] = float(PONG_H - PONG_BALL_R)
        b['vy'] = -abs(b['vy'])

    # Left paddle
    lpy = ps['paddles']['left']
    if (b['x'] - PONG_BALL_R <= 40 + PONG_PAD_W and
            b['x'] - PONG_BALL_R >= 35 and b['vx'] < 0):
        if lpy - PONG_PAD_H/2 - PONG_BALL_R <= b['y'] <= lpy + PONG_PAD_H/2 + PONG_BALL_R:
            b['vx'] = abs(b['vx']) * 1.03
            b['vy'] += ((b['y'] - lpy) / (PONG_PAD_H / 2)) * 3
            b['vy'] = max(-8.0, min(8.0, b['vy']))
            b['x'] = float(40 + PONG_PAD_W + PONG_BALL_R)

    # Right paddle
    rpy = ps['paddles']['right']
    if (b['x'] + PONG_BALL_R >= PONG_W - 40 - PONG_PAD_W and
            b['x'] + PONG_BALL_R <= PONG_W - 35 and b['vx'] > 0):
        if rpy - PONG_PAD_H/2 - PONG_BALL_R <= b['y'] <= rpy + PONG_PAD_H/2 + PONG_BALL_R:
            b['vx'] = -abs(b['vx']) * 1.03
            b['vy'] += ((b['y'] - rpy) / (PONG_PAD_H / 2)) * 3
            b['vy'] = max(-8.0, min(8.0, b['vy']))
            b['x'] = float(PONG_W - 40 - PONG_PAD_W - PONG_BALL_R)

    # Score
    if b['x'] < 0:
        ps['scores']['right'] += 1
        broadcast_all(room, {'type': 'pong_score', 'scores': ps['scores']})
        if ps['scores']['right'] >= PONG_WIN:
            end_pong(room, 'right')
            return
        reset_pong_ball(ps, 1)
    if b['x'] > PONG_W:
        ps['scores']['left'] += 1
        broadcast_all(room, {'type': 'pong_score', 'scores': ps['scores']})
        if ps['scores']['left'] >= PONG_WIN:
            end_pong(room, 'left')
            return
        reset_pong_ball(ps, -1)

    broadcast_all(room, {
        'type': 'pong_state',
        'ball': {'x': b['x'], 'y': b['y']},
        'paddles': ps['paddles'],
    })

def end_pong(room, side):
    ps = room['pong']
    ps['running'] = False
    room['finalized'] = True
    for p in room.get('players', []):
        p['_ready'] = False
    if ps.get('stop'):
        ps['stop'].set()
    winner_player = next((p for p in room['players'] if p.get('_side') == side), None)
    winner = winner_player['name'] if winner_player else f'{side} player'
    broadcast_all(room, {'type': 'pong_over', 'winner': winner, 'scores': ps['scores']})
    add_leaderboard(winner, 'Pong', ps['scores'][side] * 100)

def handle_pong_msg(client_id, room, msg):
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if not player:
        return
    t = msg.get('type')
    if t == 'pong_paddle':
        side = player.get('_side')
        if side:
            room['pong']['paddles'][side] = max(PONG_PAD_H / 2.0,
                                                 min(PONG_H - PONG_PAD_H / 2.0, float(msg.get('y', 200))))
    if t == 'pong_ready':
        player['_ready'] = True
        if (len(room['players']) == 2 and
                all(p.get('_ready') for p in room['players']) and
                not room['pong']['running']):
            room['finalized'] = False
            room['pong']['scores'] = {'left': 0, 'right': 0}
            room['pong']['paddles'] = {'left': 160.0, 'right': 160.0}
            reset_pong_ball(room['pong'], 1 if random.random() < 0.5 else -1)
            broadcast_all(room, {'type': 'pong_start', 'scores': room['pong']['scores']})
            start_pong_loop(room)

# ── Battleship ────────────────────────────────────────────────────────────────
def handle_battleship_msg(client_id, room, msg):
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if not player:
        return
    t = msg.get('type')

    if t == 'bs_place':
        ships = msg.get('ships', [])
        player['bs_grid'] = [[0]*10 for _ in range(10)]
        occupied = set()
        valid = True
        for ship in ships:
            for r, c in ship.get('cells', []):
                if not (0 <= r < 10 and 0 <= c < 10) or (r, c) in occupied:
                    valid = False
                    break
                occupied.add((r, c))
            if not valid:
                break
        if not valid:
            ws_send(client_id, {'type': 'error', 'message': 'Invalid ship layout.'})
            return
        player['bs_ships'] = ships
        for r, c in occupied:
            player['bs_grid'][r][c] = 1
        player['bs_ready'] = True
        player['bs_sunk'] = 0

        both_ready = (len(room['players']) == 2 and
                      all(p.get('bs_ready') for p in room['players']))
        if both_ready:
            room['bs_turn'] = 0
            room['bs_shots'] = {p['name']: [] for p in room['players']}
            for i, p in enumerate(room['players']):
                ws_send(p['client_id'], {'type': 'bs_battle_start', 'yourTurn': i == 0})
        else:
            ws_send(client_id, {'type': 'bs_wait_opponent'})

    if t == 'bs_fire':
        attacker_idx = room['players'].index(player)
        if attacker_idx != room['bs_turn']:
            return
        defender = room['players'][1 - attacker_idx]
        row, col = msg.get('row', 0), msg.get('col', 0)
        if not (0 <= row < 10 and 0 <= col < 10):
            return

        if defender['bs_grid'][row][col] in (2, 3):
            return

        hit = defender['bs_grid'][row][col] == 1
        defender['bs_grid'][row][col] = 2 if hit else 3
        room.setdefault('bs_shots', {}).setdefault(player['name'], []).append({'row': row, 'col': col, 'state': 2 if hit else 3})

        sunk_cells = None
        if hit:
            for ship in defender['bs_ships']:
                cells = ship.get('cells', [])
                if any(r == row and c == col for r, c in cells):
                    if all(defender['bs_grid'][r][c] == 2 for r, c in cells):
                        sunk_cells = cells
                        defender['bs_sunk'] += 1
                    break

        same_turn = bool(hit)
        ws_send(client_id, {'type': 'bs_result', 'row': row, 'col': col, 'hit': hit,
                     'sunk': bool(sunk_cells), 'sunkCells': sunk_cells, 'yourTurn': same_turn})
        ws_send(defender['client_id'], {'type': 'bs_incoming', 'row': row, 'col': col, 'hit': hit,
                                  'sunk': bool(sunk_cells), 'sunkCells': sunk_cells, 'yourTurn': not same_turn})

        if defender['bs_sunk'] >= len(defender['bs_ships']):
            room['finalized'] = True
            broadcast_all(room, {'type': 'bs_over', 'winner': player['name']})
            add_leaderboard(player['name'], 'Battleship', 1000)
        else:
            room['bs_turn'] = attacker_idx if hit else (1 - attacker_idx)

# ── Trivia ────────────────────────────────────────────────────────────────────
def start_trivia(room):
    room['finalized'] = False
    if room.get('game_running'):
        return
    if len(room['players']) < 2:
        broadcast_all(room, {'type': 'waiting', 'message': 'Need at least 2 players!', 'canStart': True})
        return
    room['game_running'] = True
    room['trivia_questions'] = shuffle(TRIVIA_QUESTIONS)[:10]
    room['trivia_round'] = 0
    for p in room['players']:
        p['score'] = 0
        p['trivia_answered'] = False
    broadcast_all(room, {'type': 'trivia_start', 'total': len(room['trivia_questions'])})
    t = threading.Timer(0.8, next_trivia_question, args=[room])
    t.daemon = True
    t.start()

def next_trivia_question(room):
    if room['trivia_round'] >= len(room['trivia_questions']):
        end_trivia(room)
        return
    q = room['trivia_questions'][room['trivia_round']]
    room['trivia_answers'] = {}
    room['trivia_deadline'] = time.time() + 15
    for p in room['players']:
        p['trivia_answered'] = False
    room['trivia_current_options'] = shuffle(q['opts'])
    broadcast_all(room, {
        'type': 'trivia_question',
        'round': room['trivia_round'] + 1,
        'total': len(room['trivia_questions']),
        'question': q['q'],
        'options': room['trivia_current_options'],
        'timeLimit': 15,
    })
    cancel_timer(room, 'trivia_timer')
    room['trivia_timer'] = threading.Timer(15.0, reveal_trivia_answer, args=[room])
    room['trivia_timer'].daemon = True
    room['trivia_timer'].start()

def reveal_trivia_answer(room):
    cancel_timer(room, 'trivia_timer')
    q = room['trivia_questions'][room['trivia_round']]
    results = [
        {'name': p['name'], 'answer': room['trivia_answers'].get(p['name']),
         'correct': room['trivia_answers'].get(p['name']) == q['a'],
         'score': p['score']}
        for p in room['players']
    ]
    broadcast_all(room, {'type': 'trivia_reveal', 'correct': q['a'],
                         'results': results, 'players': get_player_list(room)})
    room['trivia_round'] += 1
    room['trivia_current_options'] = []
    t = threading.Timer(4.0, next_trivia_question, args=[room])
    t.daemon = True
    t.start()

def end_trivia(room):
    room['game_running'] = False
    room['finalized'] = True
    room['trivia_deadline'] = 0
    sorted_p = sorted(room['players'], key=lambda p: -p['score'])
    winner = sorted_p[0] if sorted_p else None
    broadcast_all(room, {'type': 'game_over', 'winner': winner['name'] if winner else '?',
                         'players': get_player_list(room)})
    if winner:
        add_leaderboard(winner['name'], 'Trivia', winner['score'])

def handle_trivia_msg(client_id, room, msg):
    if msg.get('type') != 'trivia_answer':
        return
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if (not player or player.get('trivia_answered') or not room.get('game_running') or
            room.get('trivia_round', 0) >= len(room.get('trivia_questions', []))):
        return
    player['trivia_answered'] = True
    q = room['trivia_questions'][room['trivia_round']]
    answer = msg.get('answer')
    correct = answer == q['a']
    time_bonus = max(0, int((room['trivia_deadline'] - time.time()) / 0.15))
    points = 100 + time_bonus if correct else 0
    player['score'] += points
    room['trivia_answers'][player['name']] = answer
    ws_send(client_id, {'type': 'trivia_ack', 'correct': correct, 'points': points})
    if all(p.get('trivia_answered') for p in room['players']):
        cancel_timer(room, 'trivia_timer')
        t = threading.Timer(0.6, reveal_trivia_answer, args=[room])
        t.daemon = True
        t.start()

# ── Bomberman ─────────────────────────────────────────────────────────────────
BOMB_MAP_W, BOMB_MAP_H = 15, 13
BOMB_TILE, BOMB_WALL, BOMB_BLOCK = 0, 1, 2
BOMB_STARTS = [[0,0],[0,BOMB_MAP_W-1],[BOMB_MAP_H-1,0],[BOMB_MAP_H-1,BOMB_MAP_W-1]]
BOMB_COLORS = ['#ff4488','#44aaff','#44ff88','#ffaa00']

def create_bomb_map():
    bmap = [[BOMB_TILE]*BOMB_MAP_W for _ in range(BOMB_MAP_H)]
    for r in range(BOMB_MAP_H):
        for c in range(BOMB_MAP_W):
            if r % 2 == 1 and c % 2 == 1:
                bmap[r][c] = BOMB_WALL
    corners = [
        [0,0],[0,1],[1,0],
        [0,BOMB_MAP_W-1],[0,BOMB_MAP_W-2],[1,BOMB_MAP_W-1],
        [BOMB_MAP_H-1,0],[BOMB_MAP_H-2,0],[BOMB_MAP_H-1,1],
        [BOMB_MAP_H-1,BOMB_MAP_W-1],[BOMB_MAP_H-1,BOMB_MAP_W-2],[BOMB_MAP_H-2,BOMB_MAP_W-1],
    ]
    for r in range(BOMB_MAP_H):
        for c in range(BOMB_MAP_W):
            if bmap[r][c] != BOMB_TILE:
                continue
            if [r,c] in corners:
                continue
            if random.random() < 0.45:
                bmap[r][c] = BOMB_BLOCK
    return bmap

def get_bomb_state(room):
    return {
        'map':      room['bomb_map'],
        'players':  [{'name': p['name'], 'r': p['bomb_pos']['r'], 'c': p['bomb_pos']['c'],
                      'alive': p['alive'], 'color': p['color'],
                      'power': p['bomb_power'], 'bombMax': p['bomb_max']}
                     for p in room['players']],
        'bombs':    [{'id': b['id'], 'r': b['r'], 'c': b['c'], 'owner': b['owner']}
                     for b in room['bombs']],
        'flames':   [{'r': f['r'], 'c': f['c']} for f in room['flames']],
        'powerups': room['powerups'],
    }

def start_bomberman(room):
    room['finalized'] = False
    if room.get('game_running'):
        return
    if len(room['players']) < 2:
        broadcast_all(room, {'type': 'waiting', 'message': 'Need at least 2 players!', 'canStart': True})
        return
    room['game_running'] = True
    room['bomb_map']  = create_bomb_map()
    room['bombs']     = []
    room['flames']    = []
    room['powerups']  = []
    room['bomb_next_id'] = 0

    for i, p in enumerate(room['players']):
        sr, sc = BOMB_STARTS[i % 4]
        p['bomb_pos']   = {'r': sr, 'c': sc}
        p['bomb_power'] = 2
        p['bomb_max']   = 1
        p['bomb_count'] = 0
        p['alive']      = True
        p['color']      = BOMB_COLORS[i % 4]

    broadcast_all(room, {'type': 'bomb_start', **get_bomb_state(room)})

    stop_ev = threading.Event()
    room['bomb_stop'] = stop_ev

    def loop():
        target = time.monotonic()
        while not stop_ev.is_set() and room['game_running']:
            tick_bomberman(room)
            target += 0.2
            sleep_t = target - time.monotonic()
            if sleep_t > 0:
                time.sleep(sleep_t)

    t = threading.Thread(target=loop, daemon=True)
    t.start()

def tick_bomberman(room):
    now = time.time()
    changed = False

    # Explode ready bombs
    for b in list(room['bombs']):
        if b['timer'] <= now and not b['exploding']:
            explode_bomb(room, b)
            changed = True

    # Remove expired flames
    before = len(room['flames'])
    room['flames'] = [f for f in room['flames'] if f['until'] > now]
    if len(room['flames']) != before:
        changed = True

    # Remove exploded bombs
    room['bombs'] = [b for b in room['bombs'] if not b['exploding']]

    if changed:
        for p in room['players']:
            if not p['alive']:
                continue
            if any(f['r'] == p['bomb_pos']['r'] and f['c'] == p['bomb_pos']['c']
                   for f in room['flames']):
                p['alive'] = False
                broadcast_all(room, {'type': 'bomb_death', 'name': p['name']})

        alive = [p for p in room['players'] if p['alive']]
        if len(alive) <= 1:
            end_bomberman(room, alive[0] if alive else None)
            return

        broadcast_all(room, {'type': 'bomb_state', **get_bomb_state(room)})

def explode_bomb(room, bomb):
    bomb['exploding'] = True
    owner = next((p for p in room['players'] if p['name'] == bomb['owner']), None)
    if owner:
        owner['bomb_count'] = max(0, owner['bomb_count'] - 1)

    until = time.time() + 0.6
    dirs = [[0,0],[1,0],[-1,0],[0,1],[0,-1]]

    for dr, dc in dirs:
        max_len = 1 if (dr == 0 and dc == 0) else bomb['power']
        for i in range(max_len):
            offset = 0 if (dr == 0 and dc == 0) else i + 1
            r = bomb['r'] + dr * offset
            c = bomb['c'] + dc * offset
            if not (0 <= r < BOMB_MAP_H and 0 <= c < BOMB_MAP_W):
                break
            if room['bomb_map'][r][c] == BOMB_WALL:
                break
            room['flames'].append({'r': r, 'c': c, 'until': until})
            if room['bomb_map'][r][c] == BOMB_BLOCK:
                room['bomb_map'][r][c] = BOMB_TILE
                if random.random() < 0.3:
                    pu_type = 'power' if random.random() < 0.5 else 'bomb'
                    room['powerups'].append({'r': r, 'c': c, 'type': pu_type})
                break
            chain = next((b for b in room['bombs'] if b['r'] == r and b['c'] == c and not b['exploding']), None)
            if chain:
                chain['timer'] = 0

    room['powerups'] = [pu for pu in room['powerups']
                        if not any(f['r'] == pu['r'] and f['c'] == pu['c'] for f in room['flames'])]

def end_bomberman(room, winner):
    room['game_running'] = False
    room['finalized'] = True
    stop_ev = room.get('bomb_stop')
    if stop_ev:
        stop_ev.set()
    name = winner['name'] if winner else 'Nobody'
    broadcast_all(room, {'type': 'bomb_over', 'winner': name, 'players': get_player_list(room)})
    if winner:
        add_leaderboard(winner['name'], 'Bomberman', 500 * len(room['players']))

def handle_bomberman_msg(client_id, room, msg):
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if not player or not player.get('alive'):
        return
    t = msg.get('type')

    if t == 'bomb_move':
        now = time.time()
        if now - player.get('bomb_last_move_at', 0) < 0.06:
            return
        r, c = msg.get('r', 0), msg.get('c', 0)
        if not (0 <= r < BOMB_MAP_H and 0 <= c < BOMB_MAP_W):
            return
        if room['bomb_map'][r][c] in (BOMB_WALL, BOMB_BLOCK):
            return
        if any(b['r'] == r and b['c'] == c for b in room['bombs']):
            return
        if abs(player['bomb_pos']['r'] - r) + abs(player['bomb_pos']['c'] - c) != 1:
            return
        player['bomb_pos'] = {'r': r, 'c': c}
        player['bomb_last_move_at'] = now
        pu = next((p for p in room['powerups'] if p['r'] == r and p['c'] == c), None)
        if pu:
            if pu['type'] == 'power':
                player['bomb_power'] = min(player['bomb_power'] + 1, 6)
            else:
                player['bomb_max'] = min(player['bomb_max'] + 1, 4)
            room['powerups'] = [p for p in room['powerups'] if p is not pu]
        broadcast_all(room, {'type': 'bomb_moved', 'name': player['name'],
                             'r': r, 'c': c, 'powerups': room['powerups']})

    if t == 'bomb_place':
        if player['bomb_count'] >= player['bomb_max']:
            return
        r, c = player['bomb_pos']['r'], player['bomb_pos']['c']
        if any(b['r'] == r and b['c'] == c for b in room['bombs']):
            return
        bomb = {
            'id': room['bomb_next_id'], 'r': r, 'c': c,
            'owner': player['name'], 'power': player['bomb_power'],
            'timer': time.time() + 2.5, 'exploding': False,
        }
        room['bomb_next_id'] += 1
        room['bombs'].append(bomb)
        player['bomb_count'] += 1
        broadcast_all(room, {'type': 'bomb_placed', 'id': bomb['id'], 'r': r, 'c': c, 'owner': player['name']})

# ── Cleanup ───────────────────────────────────────────────────────────────────
def cleanup_loop():
    client_ttl = 45
    disconnect_grace = 45
    while True:
        time.sleep(10)
        now = time.time()
        stale_clients = []
        expired_disconnected = []
        with _glock:
            dead = [rid for rid, r in rooms.items() if not r['clients']]
            for rid in dead:
                del rooms[rid]
            stale_clients = [
                cid for cid, info in list(clients.items())
                if info.get('alive') and now - info.get('last_seen', now) > client_ttl
            ]
            expired_disconnected = [
                cid for cid, info in list(clients.items())
                if (not info.get('alive')) and info.get('disconnected_at') and now - info.get('disconnected_at', now) > disconnect_grace
            ]
        for cid in stale_clients:
            disconnect_client(cid)
        for cid in expired_disconnected:
            leave_room(cid)
            clients.pop(cid, None)
            broadcast_stats()

leaderboard[:] = load_leaderboard()
threading.Thread(target=cleanup_loop, daemon=True).start()

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'GameVault running on http://0.0.0.0:{port}')
    app.run(host='127.0.0.1', port=port, debug=False, threaded=True)
