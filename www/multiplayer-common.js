(function () {
  function randRoomCode() {
    return Math.random().toString(36).slice(2, 8).toUpperCase();
  }

  function normalizePlayerName(name, fallback) {
    const value = (name || '').trim();
    return value || fallback || ('Player' + Math.floor(Math.random() * 900 + 100));
  }

  function normalizeMultiplayerParams(options) {
    const opts = options || {};
    const params = new URLSearchParams(window.location.search);
    let room = (params.get('room') || '').trim().toUpperCase();
    let name = (params.get('name') || localStorage.getItem('playerName') || '').trim();

    room = room || randRoomCode();
    name = normalizePlayerName(name, opts.defaultName || 'Player');

    localStorage.setItem('playerName', name);

    const next = new URLSearchParams(window.location.search);
    if (next.get('room') !== room) next.set('room', room);
    if (next.get('name') !== name) next.set('name', name);
    const nextUrl = `${window.location.pathname}?${next.toString()}`;
    if (nextUrl !== `${window.location.pathname}${window.location.search}`) {
      history.replaceState(null, '', nextUrl);
    }

    return { roomId: room, playerName: name, params: next };
  }

  function bindPingDisplay(ws, elementOrId) {
    const el = typeof elementOrId === 'string' ? document.getElementById(elementOrId) : elementOrId;
    if (!el || !ws) return;

    const render = (ms) => {
      const n = Math.max(0, Math.round(ms || 0));
      let quality = 'GOOD';
      if (n >= 180) quality = 'POOR';
      else if (n >= 90) quality = 'OK';
      el.textContent = `Ping ${n} ms`;
      el.dataset.quality = quality.toLowerCase();
    };

    el.textContent = 'Ping -- ms';
    ws.onping = ({ latencyMs }) => render(latencyMs);
  }

  window.GameVaultMP = {
    randRoomCode,
    normalizePlayerName,
    normalizeMultiplayerParams,
    bindPingDisplay,
  };
})();
