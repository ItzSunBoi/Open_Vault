(function () {
  const openSockets = new Set();

  function closeSocketViaBeacon(clientId) {
    if (!clientId) return;
    try {
      const payload = JSON.stringify({ clientId });
      if (navigator.sendBeacon) {
        navigator.sendBeacon('/api/socket/close', new Blob([payload], { type: 'application/json' }));
        return;
      }
    } catch (_) {}
  }

  window.addEventListener('pagehide', () => {
    for (const sock of Array.from(openSockets)) {
      closeSocketViaBeacon(sock._clientId);
      sock._closed = true;
      sock.readyState = 3;
    }
    openSockets.clear();
  });

  class HTTPWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.onopen = null;
      this.onmessage = null;
      this.onclose = null;
      this.onerror = null;
      this._closed = false;
      this._clientId = null;
      this.latencyMs = null;
      this._pingTimer = null;
      openSockets.add(this);
      this._connect();
    }

    async _connect() {
      try {
        const res = await fetch('/api/socket/open', { method: 'POST' });
        const data = await res.json();
        this._clientId = data.clientId;
        this.readyState = 1;
        if (typeof this.onopen === 'function') this.onopen();
        this._startPingLoop();
        this._poll();
      } catch (err) {
        this.readyState = 3;
        if (typeof this.onerror === 'function') this.onerror(err);
        if (typeof this.onclose === 'function') this.onclose();
      }
    }

    async _poll() {
      while (!this._closed && this._clientId) {
        try {
          const res = await fetch(`/api/socket/poll?clientId=${encodeURIComponent(this._clientId)}&timeout=25`, {
            cache: 'no-store'
          });
          if (!res.ok) throw new Error('poll failed');
          const data = await res.json();
          if (data.closed) break;
          for (const message of (data.messages || [])) {
            if (typeof this.onmessage === 'function') this.onmessage({ data: message });
          }
        } catch (err) {
          if (this._closed) break;
          if (typeof this.onerror === 'function') this.onerror(err);
          await new Promise(r => setTimeout(r, 1000));
        }
      }
      if (!this._closed) {
        this.readyState = 3;
        if (this._pingTimer) clearInterval(this._pingTimer);
        openSockets.delete(this);
        if (typeof this.onclose === 'function') this.onclose();
      }
    }


    _startPingLoop() {
      const pingOnce = async () => {
        if (this._closed || !this._clientId) return;
        const startedAt = Date.now();
        try {
          const res = await fetch('/api/socket/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clientId: this._clientId, message: JSON.stringify({ type: 'ping' }) })
          });
          if (!res.ok) throw new Error('ping failed');
          const sample = Date.now() - startedAt;
          if (sample < 5000) {
            this.latencyMs = this.latencyMs == null ? sample : Math.round(this.latencyMs * 0.7 + sample * 0.3);
            if (typeof this.onping === 'function') this.onping({ latencyMs: this.latencyMs });
          }
        } catch (_) {}
      };
      pingOnce();
      this._pingTimer = setInterval(pingOnce, 10000);
    }

    async send(message) {
      if (this._closed || !this._clientId) return;
      try {
        await fetch('/api/socket/send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ clientId: this._clientId, message })
        });
      } catch (err) {
        if (typeof this.onerror === 'function') this.onerror(err);
      }
    }

    async close() {
      if (this._closed) return;
      this._closed = true;
      this.readyState = 2;
      openSockets.delete(this);
      if (this._pingTimer) clearInterval(this._pingTimer);
      const clientId = this._clientId;
      this._clientId = null;
      if (clientId) {
        try {
          await fetch('/api/socket/close', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ clientId })
          });
        } catch (_) {}
      }
      this.readyState = 3;
      if (typeof this.onclose === 'function') this.onclose();
    }
  }

  window.WebSocket = HTTPWebSocket;
})();
