import random

WORDS = [
    'ORBIT','LAVA','PIRATE','ROBOT','DRAGON','LASER','CASTLE','SHADOW','NEBULA','ECHO',
    'BRIDGE','ANCHOR','PHOENIX','TEMPLE','FALCON','MONSOON','MATRIX','TUNNEL','CIPHER','VORTEX',
    'WIZARD','GARDEN','QUARTZ','HARBOR','SATURN'
]


def init_room(room):
    room['codenames'] = {'running': False}


def start(room, ws_send):
    if len(room.get('players', [])) < 4:
        return False
    words = random.sample(WORDS, 25)
    roles = ['red'] * 9 + ['blue'] * 8 + ['neutral'] * 7 + ['assassin']
    random.shuffle(roles)
    teams = {'red': [], 'blue': []}
    for i, p in enumerate(room['players']):
        teams['red' if i % 2 == 0 else 'blue'].append(p['name'])
    spymaster = {
        'red': teams['red'][0] if teams['red'] else None,
        'blue': teams['blue'][0] if teams['blue'] else None,
    }
    room['codenames'] = {
        'running': True,
        'board': [{'word': w, 'role': r, 'revealed': False} for w, r in zip(words, roles)],
        'turn': 'red',
        'phase': 'clue',
        'clue': None,
        'teams': teams,
        'spymaster': spymaster,
        'winner': None,
    }
    _broadcast(room, ws_send)
    return True


def _player_team(state, name):
    for team in ('red', 'blue'):
        if name in state['teams'][team]:
            return team
    return None


def _view_for(state, name):
    team = _player_team(state, name)
    can_see = team and state['spymaster'].get(team) == name
    board = []
    for c in state['board']:
        board.append({'word': c['word'], 'revealed': c['revealed'], 'role': c['role'] if (can_see or c['revealed']) else None})
    return {
        'type': 'codenames_state',
        'board': board,
        'turn': state['turn'],
        'phase': state['phase'],
        'clue': state.get('clue'),
        'teams': state['teams'],
        'spymaster': state['spymaster'],
        'winner': state.get('winner'),
    }


def _broadcast(room, ws_send):
    st = room['codenames']
    for p in room['players']:
        ws_send(p['client_id'], _view_for(st, p['name']))


def handle(client_id, room, msg, ws_send):
    if msg.get('type') == 'start_game':
        start(room, ws_send)
        return
    st = room.get('codenames')
    if not st or not st.get('running'):
        return
    player = next((p for p in room['players'] if p['client_id'] == client_id), None)
    if not player:
        return
    name = player['name']
    team = _player_team(st, name)
    t = msg.get('type')
    if t == 'codenames_clue' and st['phase'] == 'clue' and team == st['turn'] and st['spymaster'].get(team) == name:
        st['clue'] = {'word': str(msg.get('word') or '')[:20], 'count': int(msg.get('count') or 1)}
        st['phase'] = 'guess'
        _broadcast(room, ws_send)
        return
    if t == 'codenames_guess' and st['phase'] == 'guess' and team == st['turn'] and st['spymaster'].get(team) != name:
        idx = int(msg.get('index', -1))
        if idx < 0 or idx >= len(st['board']) or st['board'][idx]['revealed']:
            return
        card = st['board'][idx]
        card['revealed'] = True
        role = card['role']
        if role == 'assassin':
            st['winner'] = 'blue' if st['turn'] == 'red' else 'red'
            st['running'] = False
        else:
            red_left = sum(1 for c in st['board'] if c['role'] == 'red' and not c['revealed'])
            blue_left = sum(1 for c in st['board'] if c['role'] == 'blue' and not c['revealed'])
            if red_left == 0:
                st['winner'] = 'red'; st['running'] = False
            elif blue_left == 0:
                st['winner'] = 'blue'; st['running'] = False
            elif role != st['turn']:
                st['turn'] = 'blue' if st['turn'] == 'red' else 'red'
                st['phase'] = 'clue'
                st['clue'] = None
        _broadcast(room, ws_send)
        return
    if t == 'codenames_end_turn' and team == st['turn'] and st['phase'] == 'guess':
        st['turn'] = 'blue' if st['turn'] == 'red' else 'red'
        st['phase'] = 'clue'
        st['clue'] = None
        _broadcast(room, ws_send)
