import time


class PartyService:
    def __init__(self, parties, clients, sanitize_room_id, sanitize_player_name, ws_send):
        self.parties = parties
        self.clients = clients
        self.sanitize_room_id = sanitize_room_id
        self.sanitize_player_name = sanitize_player_name
        self.ws_send = ws_send

    def create_party(self, party_id):
        return {
            'id': party_id,
            'members': {},
            'leader': None,
            'created_at': time.time(),
        }

    def _clean_member_refs(self, party):
        removed = []
        for key, member in list(party.get('members', {}).items()):
            cid = member.get('client_id')
            info = self.clients.get(cid)
            if not info or info.get('party') != party.get('id'):
                party['members'].pop(key, None)
                removed.append(cid)
        return removed

    def _assign_leader(self, party):
        members = list(party.get('members', {}).values())
        if not members:
            party['leader'] = None
            return
        current = party.get('leader')
        if current and any(m.get('client_id') == current for m in members):
            return
        # deterministic leader transfer: oldest connected client first, then lexical name
        members.sort(key=lambda m: (
            self.clients.get(m.get('client_id'), {}).get('created_at', 0),
            (m.get('name') or '').casefold(),
        ))
        party['leader'] = members[0].get('client_id')

    def remove_client(self, client_id, party_id=None):
        info = self.clients.get(client_id, {})
        party_key = self.sanitize_room_id(party_id or info.get('party'))
        if not party_key:
            if client_id in self.clients:
                self.clients[client_id]['party'] = None
            return None
        party = self.parties.get(party_key)
        if party:
            remove_keys = [
                key for key, member in list(party.get('members', {}).items())
                if member.get('client_id') == client_id
            ]
            for key in remove_keys:
                party['members'].pop(key, None)
            self._clean_member_refs(party)
            self._assign_leader(party)
            if not party.get('members'):
                self.parties.pop(party_key, None)
        if client_id in self.clients:
            self.clients[client_id]['party'] = None
        return party_key

    def join_party(self, client_id, party_id, name):
        party_key = self.sanitize_room_id(party_id)
        player_name = self.sanitize_player_name(name) or self.sanitize_player_name(self.clients.get(client_id, {}).get('name'))
        if not party_key or not player_name or client_id not in self.clients:
            return None

        current_party = self.clients[client_id].get('party')
        if current_party and current_party != party_key:
            self.remove_client(client_id, current_party)

        party = self.parties.setdefault(party_key, self.create_party(party_key))
        party['id'] = party_key
        member_key = player_name.casefold()
        existing = party['members'].get(member_key)
        old_client_id = existing.get('client_id') if existing else None
        if old_client_id and old_client_id != client_id and old_client_id in self.clients:
            self.clients[old_client_id]['party'] = None

        # Remove duplicate member entries by client_id/name
        for key, member in list(party['members'].items()):
            if member.get('client_id') == client_id or key == member_key:
                party['members'].pop(key, None)

        party['members'][member_key] = {
            'name': player_name,
            'client_id': client_id,
            'joined_at': time.time(),
        }
        self.clients[client_id]['party'] = party_key
        if not self.clients[client_id].get('name'):
            self.clients[client_id]['name'] = player_name

        self._clean_member_refs(party)
        self._assign_leader(party)
        return party_key

    def is_leader(self, party_id, client_id):
        party = self.parties.get(self.sanitize_room_id(party_id) or '')
        return bool(party and party.get('leader') == client_id)

    def get_party_stats(self, party_id, multiplayer_games):
        stats = {g: 0 for g in multiplayer_games}
        party_key = self.sanitize_room_id(party_id)
        party = self.parties.get(party_key) if party_key else None
        members = list((party or {}).get('members', {}).values())
        stats['total'] = sum(1 for member in members if self.clients.get(member.get('client_id'), {}).get('alive'))
        for member in members:
            info = self.clients.get(member.get('client_id'), {})
            if not info.get('alive'):
                continue
            game = info.get('game')
            if game in stats:
                stats[game] += 1
        return stats

    def get_payload(self, party_id, multiplayer_games):
        party_key = self.sanitize_room_id(party_id)
        party = self.parties.get(party_key) if party_key else None
        members = []
        if party:
            self._clean_member_refs(party)
            self._assign_leader(party)
            for member in party.get('members', {}).values():
                info = self.clients.get(member.get('client_id'), {})
                members.append({
                    'name': member.get('name'),
                    'online': bool(info.get('alive')),
                    'game': info.get('game'),
                    'room': info.get('room'),
                    'isLeader': member.get('client_id') == party.get('leader'),
                })
        members.sort(key=lambda m: (not m['online'], not m.get('isLeader'), (m.get('name') or '').casefold()))
        return {
            'type': 'party_state',
            'party': party_key,
            'leader': next((m.get('name') for m in members if m.get('isLeader')), None),
            'members': members,
            'stats': self.get_party_stats(party_key, multiplayer_games),
        }

    def broadcast_party_state(self, party_id, multiplayer_games):
        payload = self.get_payload(party_id, multiplayer_games)
        if not payload.get('party'):
            return
        party = self.parties.get(payload['party'])
        if not party:
            return
        for member in list(party.get('members', {}).values()):
            self.ws_send(member.get('client_id'), payload)
