import random
import time


WIDTH, HEIGHT = 800, 500
SPEED = 3
TURN = 0.14


def init_room(room):
    room['curvefever'] = {'running': False, 'last_tick': 0}


def start(room, ws_send):
    if len(room.get('players', [])) < 2:
        return False
    players = []
    for idx, p in enumerate(room['players']):
        players.append({
            'name': p['name'],
            'x': 100 + idx * 80,
            'y': 100 + idx * 60,
            'a': random.random() * 6.28,
            'alive': True,
            'left': False,
            'right': False,
            'trail': [],
        })
    room['curvefever'] = {'running': True, 'players': players, 'last_tick': time.time(), 'winner': None}
    _broadcast(room, ws_send)
    return True


def _broadcast(room, ws_send):
    st = room['curvefever']
    payload = {'type': 'curve_state', 'running': st['running'], 'players': st.get('players', []), 'winner': st.get('winner')}
    for p in room['players']:
        ws_send(p['client_id'], payload)


def handle(client_id, room, msg, ws_send):
    t = msg.get('type')
    if t == 'start_game':
        start(room, ws_send)
        return
    st = room.get('curvefever')
    if not st or not st.get('running'):
        return
    p = next((pl for pl in st['players'] if pl['name'] == next((rp['name'] for rp in room['players'] if rp['client_id'] == client_id), None)), None)
    if not p:
        return
    if t == 'curve_input':
        p['left'] = bool(msg.get('left'))
        p['right'] = bool(msg.get('right'))
    elif t == 'curve_tick':
        _tick(room, ws_send)


def _tick(room, ws_send):
    st = room['curvefever']
    now = time.time()
    if now - st['last_tick'] < 0.04:
        return
    st['last_tick'] = now
    occupied = set()
    for p in st['players']:
        for x, y in p['trail']:
            occupied.add((int(x), int(y)))
    for p in st['players']:
        if not p['alive']:
            continue
        if p['left']:
            p['a'] -= TURN
        if p['right']:
            p['a'] += TURN
        p['x'] += SPEED * __import__('math').cos(p['a'])
        p['y'] += SPEED * __import__('math').sin(p['a'])
        key = (int(p['x']), int(p['y']))
        if p['x'] < 0 or p['x'] >= WIDTH or p['y'] < 0 or p['y'] >= HEIGHT or key in occupied:
            p['alive'] = False
            continue
        p['trail'].append([p['x'], p['y']])
        if len(p['trail']) > 600:
            p['trail'] = p['trail'][-600:]
        occupied.add(key)
    alive = [p for p in st['players'] if p['alive']]
    if len(alive) <= 1:
        st['running'] = False
        st['winner'] = alive[0]['name'] if alive else None
    _broadcast(room, ws_send)
