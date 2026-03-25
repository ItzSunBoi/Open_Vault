import random

COLORS = ['R', 'G', 'B', 'Y']


def build_deck():
    deck = []
    for c in COLORS:
        for n in range(0, 10):
            deck.extend([f'{c}{n}'] * (2 if n else 1))
        deck.extend([f'{c}S', f'{c}R', f'{c}D2'] * 2)
    deck.extend(['W', 'WD4'] * 4)
    random.shuffle(deck)
    return deck


def init_room(room):
    room['uno'] = {'running': False, 'deck': [], 'discard': [], 'hands': {}, 'turn': 0, 'dir': 1, 'pending_draw': 0}


def start(room, ws_send):
    if len(room.get('players', [])) < 2:
        return False
    init_room(room)
    u = room['uno']
    u['running'] = True
    u['deck'] = build_deck()
    for p in room['players']:
        u['hands'][p['name']] = [u['deck'].pop() for _ in range(7)]
    first = u['deck'].pop()
    while first.startswith('W') and u['deck']:
        u['deck'].insert(0, first)
        first = u['deck'].pop()
    u['discard'] = [first]
    _broadcast_state(room, ws_send)
    return True


def _next_idx(room, step=1):
    u = room['uno']
    return (u['turn'] + (u['dir'] * step)) % len(room['players'])


def _can_play(card, top):
    if card.startswith('W'):
        return True
    return card[0] == top[0] or card[1:] == top[1:]


def _player_name(room, client_id):
    for p in room['players']:
        if p['client_id'] == client_id:
            return p['name']
    return None


def _private_state(room, player_name):
    u = room['uno']
    top = u['discard'][-1] if u['discard'] else None
    return {
        'type': 'uno_state',
        'running': u['running'],
        'topCard': top,
        'turn': room['players'][u['turn']]['name'] if room.get('players') else None,
        'hand': u['hands'].get(player_name, []),
        'counts': {name: len(hand) for name, hand in u['hands'].items()},
        'direction': u['dir'],
    }


def _broadcast_state(room, ws_send):
    for p in room['players']:
        ws_send(p['client_id'], _private_state(room, p['name']))


def _draw(u, name, n=1):
    for _ in range(n):
        if not u['deck'] and len(u['discard']) > 1:
            top = u['discard'].pop()
            random.shuffle(u['discard'])
            u['deck'] = u['discard']
            u['discard'] = [top]
        if u['deck']:
            u['hands'][name].append(u['deck'].pop())


def handle(client_id, room, msg, ws_send):
    t = msg.get('type')
    if t == 'start_game':
        start(room, ws_send)
        return
    u = room.get('uno')
    if not u or not u.get('running'):
        return
    pname = _player_name(room, client_id)
    if not pname:
        return
    turn_name = room['players'][u['turn']]['name']
    if t == 'uno_draw' and pname == turn_name:
        _draw(u, pname, max(1, u.get('pending_draw', 1)))
        u['pending_draw'] = 0
        u['turn'] = _next_idx(room)
        _broadcast_state(room, ws_send)
        return
    if t != 'uno_play' or pname != turn_name:
        return
    card = str(msg.get('card') or '')
    choose_color = str(msg.get('color') or random.choice(COLORS))[0:1].upper()
    hand = u['hands'].get(pname, [])
    if card not in hand:
        return
    top = u['discard'][-1]
    if not _can_play(card, top):
        return
    hand.remove(card)
    played = card if not card.startswith('W') else f'{choose_color}{card}'
    u['discard'].append(played)

    step = 1
    if card.endswith('S'):
        step = 2
    elif card.endswith('R'):
        u['dir'] *= -1
        if len(room['players']) == 2:
            step = 2
    elif card.endswith('D2'):
        target = room['players'][_next_idx(room)]['name']
        _draw(u, target, 2)
        step = 2
    elif card == 'WD4':
        target = room['players'][_next_idx(room)]['name']
        _draw(u, target, 4)
        step = 2

    if not hand:
        u['running'] = False
        for p in room['players']:
            ws_send(p['client_id'], {'type': 'uno_over', 'winner': pname})
        return
    u['turn'] = _next_idx(room, step)
    _broadcast_state(room, ws_send)
