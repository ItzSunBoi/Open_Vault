ROWS, COLS = 6, 7


def init_room(room):
    room['c4'] = {
        'board': [[None for _ in range(COLS)] for _ in range(ROWS)],
        'turn': 0,
        'running': False,
        'winner': None,
        'draw': False,
    }


def start(room, ws_send):
    if len(room.get('players', [])) != 2:
        return False
    init_room(room)
    room['c4']['running'] = True
    for idx, p in enumerate(room['players'][:2]):
        ws_send(p['client_id'], {'type': 'joined', 'piece': 'R' if idx == 0 else 'Y', 'players': _players(room)})
    _broadcast(room, ws_send, {'type': 'c4_start', 'state': public_state(room)})
    return True


def _players(room):
    out = []
    for i, p in enumerate(room.get('players', [])):
        out.append({'name': p['name'], 'piece': 'R' if i == 0 else 'Y'})
    return out


def _broadcast(room, ws_send, payload):
    for p in room.get('players', []):
        ws_send(p['client_id'], payload)


def public_state(room):
    s = room['c4']
    current = room['players'][s['turn']]['name'] if room.get('players') and s['running'] else None
    return {
        'board': s['board'],
        'turn': current,
        'winner': s['winner'],
        'draw': s['draw'],
        'running': s['running'],
    }


def handle(client_id, room, msg, ws_send):
    if msg.get('type') == 'start_game':
        start(room, ws_send)
        return
    if msg.get('type') != 'c4_drop':
        return
    s = room.get('c4') or {}
    if not s.get('running'):
        return
    if room['players'][s['turn']]['client_id'] != client_id:
        return
    col = int(msg.get('col', -1))
    if col < 0 or col >= COLS:
        return
    row = next((r for r in range(ROWS - 1, -1, -1) if s['board'][r][col] is None), None)
    if row is None:
        return
    piece = 'R' if s['turn'] == 0 else 'Y'
    s['board'][row][col] = piece
    if _won(s['board'], row, col, piece):
        s['winner'] = room['players'][s['turn']]['name']
        s['running'] = False
    elif all(s['board'][0][c] is not None for c in range(COLS)):
        s['draw'] = True
        s['running'] = False
    else:
        s['turn'] = (s['turn'] + 1) % 2
    _broadcast(room, ws_send, {'type': 'c4_state', 'state': public_state(room)})


def _won(board, row, col, piece):
    for dr, dc in ((1,0),(0,1),(1,1),(1,-1)):
        count = 1
        for sign in (-1, 1):
            r, c = row + dr * sign, col + dc * sign
            while 0 <= r < ROWS and 0 <= c < COLS and board[r][c] == piece:
                count += 1
                r += dr * sign
                c += dc * sign
        if count >= 4:
            return True
    return False
