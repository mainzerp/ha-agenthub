/*
 * agenthub-activity-orb-card - Home Assistant Lovelace card around <agenthub-activity-orb>.
 *
 * Source of the card wrapper. It is NOT loaded by the container dashboard: scripts/sync_orb_card.py
 * concatenates activity-orb.js and this file into the single deliverable
 * custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js (the widget must be defined first).
 *
 * Plain HTMLElement with Shadow DOM (LitElement is not available in a custom card). Data arrives over the
 * Home Assistant WebSocket through the bridge command "ha_agenthub/activity/subscribe":
 *   {type: "snapshot", agents: [{agent_id, name, last_used}], connected: bool}   first message
 *   {type: "activity", event: <activity payload>}                                 span_start / span_end
 *   {type: "status", connected: bool}                                             container link state
 * Config: title (string, default "Agent Activity"), compact (boolean), demo (boolean), entry_id (string, optional:
 * passed to the subscribe command to pick a config entry; without it the bridge uses its first loaded entry).
 *
 * Self-healing: when the bridge reloads its config entry it sends {status connected:false} and then nothing, while
 * the client-side subscription still looks alive. If the card stays offline for OFFLINE_RESUBSCRIBE_MS it
 * unsubscribes and subscribes again; an error response (for example not_loaded) is retried with backoff.
 */
(function () {
    'use strict';

    var CARD_TAG = 'agenthub-activity-orb-card';
    if (typeof window === 'undefined' || !window.customElements || window.customElements.get(CARD_TAG)) {
        return;
    }

    var SUBSCRIBE_MESSAGE = { type: 'ha_agenthub/activity/subscribe' };
    var BACKOFF_START_MS = 5000;
    var BACKOFF_MAX_MS = 120000;
    var OFFLINE_RESUBSCRIBE_MS = 30000;

    var STYLE = [
        ':host { display: block; height: 100%; }',
        'ha-card { overflow: hidden; height: 100%; display: flex; flex-direction: column; }',
        '.header { padding: 12px 16px 0; font-family: var(--ha-card-header-font-family, inherit);',
        '  font-size: var(--ha-card-header-font-size, 24px); font-weight: var(--ha-font-weight-normal, 400);',
        '  line-height: var(--ha-line-height-condensed, 32px); letter-spacing: -0.012em; color: var(--ha-card-header-color, var(--primary-text-color));',
        '  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }',
        '.header[hidden] { display: none; }',
        '.body { padding: 12px 16px 16px; flex: 1; min-height: 0; min-width: 0; }',
        'agenthub-activity-orb { display: block; width: 100%; min-width: 0; }'
    ].join('\n');

    // Colour of an HA theme variable as [r, g, b], or null when unresolved.
    function resolveColor(host, name) {
        var raw = getComputedStyle(host).getPropertyValue(name).trim();
        if (!raw) { return null; }
        try {
            var c = document.createElement('canvas').getContext('2d');
            c.fillStyle = '#000001';
            c.fillStyle = raw;
            var v = c.fillStyle;
            if (v.charAt(0) === '#' && v.length === 7) {
                return [parseInt(v.slice(1, 3), 16), parseInt(v.slice(3, 5), 16), parseInt(v.slice(5, 7), 16)];
            }
            var m = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/.exec(v);
            if (m) { return [+m[1], +m[2], +m[3]]; }
        } catch (_) { /* unresolved */ }
        return null;
    }

    function luminance(rgb) {
        return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
    }

    function mixWhite(rgb, t) {
        return 'rgb(' + rgb.map(function (v) { return Math.round(v + (255 - v) * t); }).join(',') + ')';
    }

    function validateConfig(config) {
        if (!config || typeof config !== 'object') { throw new Error('Invalid configuration'); }
        if (config.title !== undefined && typeof config.title !== 'string') { throw new Error('"title" must be a string'); }
        if (config.compact !== undefined && typeof config.compact !== 'boolean') { throw new Error('"compact" must be a boolean'); }
        if (config.demo !== undefined && typeof config.demo !== 'boolean') { throw new Error('"demo" must be a boolean'); }
        if (config.entry_id !== undefined && typeof config.entry_id !== 'string') { throw new Error('"entry_id" must be a string'); }
    }

    class AgentHubActivityOrbCard extends HTMLElement {
        constructor() {
            super();
            this._config = null;
            this._hass = null;
            this._conn = null;
            this._unsub = null;
            this._subscribing = false;
            this._token = 0;
            this._retryTimer = 0;
            this._offlineTimer = 0;
            this._backoff = BACKOFF_START_MS;
            this._attached = false;
            this._themeSig = '';
            this._connListeners = null;
            var root = this.attachShadow({ mode: 'open' });
            var style = document.createElement('style');
            style.textContent = STYLE;
            root.appendChild(style);
            this._card = document.createElement('ha-card');
            this._header = document.createElement('div');
            this._header.className = 'header';
            var body = document.createElement('div');
            body.className = 'body';
            this._orb = document.createElement('agenthub-activity-orb');
            this._orb.setAttribute('transport', 'external');
            this._orb.setAttribute('bare', '');
            body.appendChild(this._orb);
            this._card.appendChild(this._header);
            this._card.appendChild(body);
            root.appendChild(this._card);
        }

        /* ------------------------------ Lovelace card API ------------------------------ */

        static getStubConfig() {
            return { title: 'Agent Activity' };
        }

        setConfig(config) {
            validateConfig(config);
            var prev = this._config;
            this._config = {
                title: config.title !== undefined ? config.title : 'Agent Activity',
                compact: config.compact === true,
                demo: config.demo === true,
                entry_id: config.entry_id || ''
            };
            this._header.textContent = this._config.title;
            this._header.hidden = this._config.title === '';
            this._orb.toggleAttribute('compact', this._config.compact);
            this._orb.toggleAttribute('demo', this._config.demo);
            if (prev && (prev.demo !== this._config.demo || prev.entry_id !== this._config.entry_id) && this._attached) {
                this._teardown();
                this._setup();
            }
        }

        getCardSize() {
            return this._config && this._config.compact ? 3 : 5;
        }

        getGridOptions() {
            if (this._config && this._config.compact) {
                return { columns: 12, rows: 3, min_columns: 6, min_rows: 2, max_rows: 5 };
            }
            return { columns: 12, rows: 5, min_columns: 6, min_rows: 4, max_rows: 6 };
        }

        set hass(hass) {
            this._hass = hass;
            var sig = hass && hass.themes ? String(hass.themes.theme) + '|' + String(hass.themes.darkMode) : '';
            if (sig !== this._themeSig) {
                this._themeSig = sig;
                // HA applies the new theme variables right after assigning hass.
                var self = this;
                requestAnimationFrame(function () { self._applyTheme(); });
            }
            if (this._attached) { this._setup(); }
        }

        get hass() { return this._hass; }

        connectedCallback() {
            this._attached = true;
            this._applyTheme();
            this._setup();
        }

        disconnectedCallback() {
            this._attached = false;
            this._teardown();
        }

        /* ------------------------------ theme mapping ------------------------------ */

        // The orb is designed for a dark stage: the card chrome follows the HA theme, the stage stays dark.
        // HA tokens are mapped onto the widget's tokens as inline custom properties on the widget host:
        //   --blue (orchestrator)  <- --info-color, else --primary-color, lightened so it reads on dark
        //   --danger (errors)      <- --error-color
        //   --success (ok flash)   <- --success-color
        //   --color-mist / --color-dust (label text) <- --secondary-text-color / --disabled-text-color,
        //                            only when the theme is dark (light themes have dark text, unreadable on the stage)
        _applyTheme() {
            var host = this._orb;
            if (!host) { return; }
            var set = function (name, value) {
                if (value) { host.style.setProperty(name, value); } else { host.style.removeProperty(name); }
            };
            var info = resolveColor(this, '--info-color') || resolveColor(this, '--primary-color');
            set('--blue', info ? mixWhite(info, luminance(info) < 0.55 ? 0.4 : 0.15) : '');
            var err = resolveColor(this, '--error-color');
            set('--danger', err ? mixWhite(err, luminance(err) < 0.4 ? 0.2 : 0) : '');
            var ok = resolveColor(this, '--success-color');
            set('--success', ok ? mixWhite(ok, luminance(ok) < 0.4 ? 0.2 : 0) : '');
            var secondary = resolveColor(this, '--secondary-text-color');
            var disabled = resolveColor(this, '--disabled-text-color');
            var darkTheme = secondary && luminance(secondary) > 0.5;
            set('--color-mist', darkTheme ? 'rgb(' + secondary.join(',') + ')' : '');
            set('--color-dust', darkTheme && disabled && luminance(disabled) > 0.3 ? 'rgb(' + disabled.join(',') + ')' : '');
        }

        /* ------------------------------ subscription ------------------------------ */

        _setup() {
            var cfg = this._config;
            if (!cfg || !this._attached) { return; }
            if (cfg.demo) { return; }
            var hass = this._hass;
            if (!hass || !hass.connection) { return; }
            if (this._conn !== hass.connection) {
                // A new connection object (full reload): drop the old subscription and listeners first.
                this._teardown();
                this._conn = hass.connection;
                this._listenConnection(hass.connection);
            }
            if (this._unsub || this._subscribing || this._retryTimer) { return; }
            this._subscribe();
        }

        _listenConnection(conn) {
            if (!conn || typeof conn.addEventListener !== 'function') { return; }
            var self = this;
            var onDisconnected = function () { self._setState('reconnecting'); };
            var onReconnectError = function () { self._setState('reconnecting'); };
            // On "ready" HA has replayed the subscription; the bridge then sends a fresh snapshot (state -> live).
            conn.addEventListener('disconnected', onDisconnected);
            conn.addEventListener('reconnect-error', onReconnectError);
            this._connListeners = { conn: conn, onDisconnected: onDisconnected, onReconnectError: onReconnectError };
        }

        _subscribe() {
            var self = this;
            var token = this._token;
            this._subscribing = true;
            var promise;
            var command = { type: SUBSCRIBE_MESSAGE.type };
            if (this._config && this._config.entry_id) { command.entry_id = this._config.entry_id; }
            try {
                promise = this._conn.subscribeMessage(function (msg) { self._onMessage(msg); }, command);
            } catch (err) {
                promise = Promise.reject(err);
            }
            Promise.resolve(promise).then(function (unsub) {
                self._subscribing = false;
                if (token !== self._token || !self._attached) {
                    // Detached (or reconfigured) while the command was in flight: undo it.
                    if (typeof unsub === 'function') { try { unsub(); } catch (_) { /* ignore */ } }
                    return;
                }
                self._unsub = unsub;
            }, function () {
                self._subscribing = false;
                if (token !== self._token || !self._attached) { return; }
                // Error response (unknown command on an older bridge, not_loaded, ...): stay offline and retry with backoff.
                self._setState('offline', true);
                var delay = self._backoff;
                self._backoff = Math.min(BACKOFF_MAX_MS, self._backoff * 2);
                self._retryTimer = setTimeout(function () {
                    self._retryTimer = 0;
                    self._setup();
                }, delay);
            });
        }

        _onMessage(msg) {
            if (!msg || typeof msg !== 'object') { return; }
            this._backoff = BACKOFF_START_MS;
            if (msg.type === 'snapshot') {
                this._orb.applySnapshot({ agents: Array.isArray(msg.agents) ? msg.agents : [] });
                // connected:false in a snapshot is never permanent: a status message follows when upstream connects.
                this._setState(msg.connected === false ? 'offline' : 'live');
            } else if (msg.type === 'activity') {
                if (msg.event) { this._orb.pushEvent(msg.event); }
            } else if (msg.type === 'status') {
                this._setState(msg.connected ? 'live' : 'offline');
            }
        }

        // Drive the widget indicator; an offline state that lasts too long triggers a resubscribe (self-healing).
        _setState(state, fromRetryPath) {
            this._orb.setConnectionState(state);
            if (state === 'offline' && !fromRetryPath) {
                if (!this._offlineTimer) {
                    var self = this;
                    this._offlineTimer = setTimeout(function () {
                        self._offlineTimer = 0;
                        self._resubscribe();
                    }, OFFLINE_RESUBSCRIBE_MS);
                }
            } else if (this._offlineTimer) {
                clearTimeout(this._offlineTimer);
                this._offlineTimer = 0;
            }
        }

        // The server side subscription may be gone while the client handle still looks alive
        // (bridge config entry reload): drop it and subscribe again.
        _resubscribe() {
            if (!this._attached || !this._config || this._config.demo || !this._conn) { return; }
            this._unsubscribe();
            this._subscribe();
        }

        _unsubscribe() {
            this._token += 1;
            if (this._retryTimer) { clearTimeout(this._retryTimer); this._retryTimer = 0; }
            if (this._offlineTimer) { clearTimeout(this._offlineTimer); this._offlineTimer = 0; }
            if (this._unsub) {
                var unsub = this._unsub;
                this._unsub = null;
                try {
                    var r = unsub();
                    if (r && typeof r.catch === 'function') { r.catch(function () { /* ignore */ }); }
                } catch (_) { /* ignore */ }
            }
            this._subscribing = false;
        }

        _teardown() {
            this._unsubscribe();
            this._backoff = BACKOFF_START_MS;
            var l = this._connListeners;
            if (l && l.conn && typeof l.conn.removeEventListener === 'function') {
                l.conn.removeEventListener('disconnected', l.onDisconnected);
                l.conn.removeEventListener('reconnect-error', l.onReconnectError);
            }
            this._connListeners = null;
            this._conn = null;
        }
    }

    window.customElements.define(CARD_TAG, AgentHubActivityOrbCard);

    window.customCards = window.customCards || [];
    if (!window.customCards.some(function (c) { return c && c.type === CARD_TAG; })) {
        window.customCards.push({
            type: CARD_TAG,
            name: 'Agent Activity Orb',
            description: 'Live view of the HA-AgentHub orchestrator and the agents it dispatches to.',
            preview: true
        });
    }
})();
