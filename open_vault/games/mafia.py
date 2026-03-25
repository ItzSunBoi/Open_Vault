import random

ROLES = ['werewolf', 'doctor', 'detective']


def init_room(room):
    room['mafia'] = {'running': False}


def start(room, ws_send):
    if len(room.get('players', [])) < 4:
        return False
    players = [p['name'] for p in room['players']]
    roles = ['villager'] * len(players)
    picks = random.sample(range(len(players)), k=min(len(ROLES), len(players)))
    for i, idx in enumerate(picks):
        roles[idx] = ROLES[i]
    room['mafia'] = {
        'running': True,
        'phase': 'night',
        'day': 1,
        'alive': {n: True for n in players},
        'roles': dict(zip(players, roles)),
        'votes': {},
        'night_actions': {},
        'log': [],
        'winner': None,
    }
    _broadcast(room, ws_send)
    return True


def _broadcast(room, ws_send):
    st = room['mafia']
    for p in room['players']:
        n = p['name']
        payload = {
            'type': 'mafia_state',
            'phase': st['phase'],
            'day': st['day'],
            'alive': st['alive'],
            'players': [pl['name'] for pl in room['players']],
            'role': st['roles'].get(n),
            'log': st['log'][-5:],
            'winner': st.get('winner'),
        }
        if st['phase'] == 'night' and st['roles'].get(n) == 'werewolf':
            payload['werewolves'] = [name for name, role in st['roles'].items() if role == 'werewolf' and st['alive'].get(name)]
        ws_send(p['client_id'], payload)


def _check_winner(st):
    wolves = [n for n, r in st['roles'].items() if r == 'werewolf' and st['alive'].get(n)]
    villagers = [n for n, r in st['roles'].items() if r != 'werewolf' and st['alive'].get(n)]
    if not wolves:
        st['winner'] = 'villagers'; st['running'] = False
    elif len(wolves) >= len(villagers):
        st['winner'] = 'werewolves'; st['running'] = False


def handle(client_id, room, msg, ws_send):
    if msg.get('type') == 'start_game':
        start(room, ws_send)
        return
    st = room.get('mafia')
    if not st or not st.get('running'):
        return
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if not player:
        return
    name = player['name']
    if not st['alive'].get(name):
        return
    t = msg.get('type')
    if st['phase'] == 'night' and t == 'mafia_night_action':
        st['night_actions'][name] = str(msg.get('target') or '')
        alive_count = sum(1 for n in st['alive'] if st['alive'][n])
        if len(st['night_actions']) >= alive_count:
            _resolve_night(st)
        _broadcast(room, ws_send)
    elif st['phase'] == 'day' and t == 'mafia_vote':
        target = str(msg.get('target') or '')
        if st['alive'].get(target):
            st['votes'][name] = target
        alive_count = sum(1 for n in st['alive'] if st['alive'][n])
        if len(st['votes']) >= alive_count:
            _resolve_day(st)
        _broadcast(room, ws_send)


def _resolve_night(st):
    wolves = [n for n, r in st['roles'].items() if r == 'werewolf' and st['alive'].get(n)]
    wolf_targets = [st['night_actions'].get(n) for n in wolves if st['alive'].get(st['night_actions'].get(n))]
    victim = max(set(wolf_targets), key=wolf_targets.count) if wolf_targets else None
    doc = next((n for n, r in st['roles'].items() if r == 'doctor' and st['alive'].get(n)), None)
    saved = st['night_actions'].get(doc) if doc else None
    det = next((n for n, r in st['roles'].items() if r == 'detective' and st['alive'].get(n)), None)
    d_target = st['night_actions'].get(det) if det else None
    if d_target:
        is_wolf = st['roles'].get(d_target) == 'werewolf'
        st['log'].append(f'Detective checked {d_target}: {"Werewolf" if is_wolf else "Not Werewolf"}.')
    if victim and victim != saved and st['alive'].get(victim):
        st['alive'][victim] = False
        st['log'].append(f'{victim} was eliminated during the night.')
    else:
        st['log'].append('No one was eliminated at night.')
    st['night_actions'] = {}
    _check_winner(st)
    if st.get('running'):
        st['phase'] = 'day'
        st['votes'] = {}


def _resolve_day(st):
    tally = {}
    for target in st['votes'].values():
        tally[target] = tally.get(target, 0) + 1
    if tally:
        kicked = max(tally, key=tally.get)
        st['alive'][kicked] = False
        st['log'].append(f'{kicked} was voted out by the village.')
    st['votes'] = {}
    _check_winner(st)
    if st.get('running'):
        st['phase'] = 'night'
        st['day'] += 1
