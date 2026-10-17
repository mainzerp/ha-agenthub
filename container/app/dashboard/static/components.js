(function () {
    'use strict';

    /* === dashboardShell === */
    var dashboardShell = function () {
        return {
            sidebarOpen: true,
            isMobile: false,
            init() {
                var mql = window.matchMedia('(max-width: 768px)');
                var apply = function () {
                    this.isMobile = mql.matches;
                    if (this.isMobile) {
                        this.sidebarOpen = false;
                    } else {
                        this.sidebarOpen = true;
                    }
                }.bind(this);

                apply();
                if (mql.addEventListener) {
                    mql.addEventListener('change', apply);
                } else if (mql.addListener) {
                    mql.addListener(apply);
                }

                var touchStartX = 0;
                document.addEventListener('touchstart', function (e) {
                    touchStartX = e.changedTouches[0].screenX;
                });
                document.addEventListener('touchend', function (e) {
                    var dx = e.changedTouches[0].screenX - touchStartX;
                    if (dx > 50 && touchStartX < 30 && this.isMobile) { this.sidebarOpen = true; }
                    if (dx < -50 && this.isMobile && this.sidebarOpen) { this.sidebarOpen = false; }
                }.bind(this));
            },
            get sidebarInert() {
                return this.isMobile && !this.sidebarOpen;
            }
        };
    };

    /* === dashModal === */
    var dashModal = function (initialOpen) {
        return {
            open: !!initialOpen,
            _previouslyFocused: null,
            show() {
                this._previouslyFocused = document.activeElement;
                this.open = true;
                this.$nextTick(function () {
                    var root = this.$refs.modalRoot;
                    if (!root) return;
                    var focusable = root.querySelector(
                        '[autofocus], button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
                    );
                    if (focusable) {
                        focusable.focus();
                    }
                }.bind(this));
            },
            hide() {
                this.open = false;
                if (this._previouslyFocused && this._previouslyFocused.focus) {
                    this._previouslyFocused.focus();
                }
            },
            onKeydown(e) {
                if (!this.open) return;
                if (e.key === 'Escape') {
                    e.stopPropagation();
                    this.hide();
                }
            }
        };
    };

    /* === dashToasts === */
    var dashToasts = function () {
        return {
            toasts: [],
            push(message, kind) {
                var id = Date.now() + Math.random();
                this.toasts.push({ id: id, message: message, kind: kind });
                setTimeout(function () {
                    this.toasts = this.toasts.filter(function (t) { return t.id !== id; });
                }.bind(this), 4000);
            }
        };
    };

    /* === dashPage === */
    var dashPage = function (opts) {
        opts = opts || {};
        return {
            _pollTimers: [],
            init() {
                if (opts.mount) {
                    opts.mount.call(this);
                }
            },
            destroy() {
                this._pollTimers.forEach(function (t) { clearInterval(t); });
                this._pollTimers = [];
                if (opts.unmount) {
                    opts.unmount.call(this);
                }
            },
            pollEvery(intervalMs, fn) {
                var t = setInterval(fn.bind(this), intervalMs);
                this._pollTimers.push(t);
                return t;
            }
        };
    };

    /* === dashDataTable === */
    var dashDataTable = function (opts) {
        opts = opts || {};
        return {
            rows: opts.rows || [],
            sortKey: opts.sortKey || '',
            sortAsc: opts.sortAsc !== false,
            pageNum: 1,
            pageSize: opts.pageSize || 25,
            filter: '',
            get filteredRows() {
                var rows = this.rows.slice();
                if (this.filter) {
                    var f = this.filter.toLowerCase();
                    rows = rows.filter(function (r) {
                        return Object.values(r).some(function (v) {
                            return String(v).toLowerCase().indexOf(f) !== -1;
                        });
                    });
                }
                if (this.sortKey) {
                    var k = this.sortKey;
                    var asc = this.sortAsc;
                    rows.sort(function (a, b) {
                        var av = a[k], bv = b[k];
                        if (av < bv) return asc ? -1 : 1;
                        if (av > bv) return asc ? 1 : -1;
                        return 0;
                    });
                }
                return rows;
            },
            get pagedRows() {
                var start = (this.pageNum - 1) * this.pageSize;
                return this.filteredRows.slice(start, start + this.pageSize);
            },
            get totalPages() {
                return Math.max(1, Math.ceil(this.filteredRows.length / this.pageSize));
            },
            toggleSort(key) {
                if (this.sortKey === key) {
                    this.sortAsc = !this.sortAsc;
                } else {
                    this.sortKey = key;
                    this.sortAsc = true;
                }
                this.pageNum = 1;
            }
        };
    };

    /* === dashSidebarGroups === */
    var dashSidebarGroups = function (initialState) {
        var storageKey = 'dash.sidebar.groups';
        var saved = {};
        try {
            saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
        } catch (_) { }
        return {
            groups: Object.assign({}, initialState || {}, saved),
            toggle(key) {
                this.groups[key] = !this.groups[key];
                try {
                    localStorage.setItem(storageKey, JSON.stringify(this.groups));
                } catch (_) { }
            },
            isOpen(key) {
                return !!this.groups[key];
            }
        };
    };

    /* === dashCommandPalette === */
    var dashCommandPalette = function () {
        return {
            open: false,
            query: '',
            selectedIndex: 0,
            allCommands: [],
            pageCommands: [],
            init() {
                var self = this;
                this.buildCommands();
                this.$watch('query', function () {
                    this.selectedIndex = 0;
                }.bind(this));
                document.addEventListener('keydown', function (e) {
                    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                        e.preventDefault();
                        self.open = true;
                        self.$nextTick(function () {
                            var input = self.$refs.cmdkInput;
                            if (input) input.focus();
                        });
                    }
                    if (e.key === 'Escape' && self.open) {
                        self.open = false;
                    }
                });
            },
            buildCommands() {
                var cmds = [];
                var navData = document.getElementById('dash-nav-data');
                if (navData) {
                    try {
                        var groups = JSON.parse(navData.textContent);
                        groups.forEach(function (g) {
                            g.items.forEach(function (item) {
                                var itemHref = item.href || ('/dashboard/' + item.href_name.replace('_page', '').replace(/_/g, '-'));
                                cmds.push({
                                    label: item.label,
                                    group: g.label,
                                    keywords: item.label + ' ' + g.label,
                                    action: 'navigate',
                                    href: window.dashUrl(itemHref)
                                });
                            });
                        });
                    } catch (_) { }
                }
                cmds.push(
                    { label: 'Flush all caches', group: 'Action', keywords: 'cache flush all', action: 'api', method: 'POST', url: '/api/admin/cache/flush' },
                    { label: 'Flush routing cache', group: 'Action', keywords: 'cache flush routing', action: 'api', method: 'POST', url: '/api/admin/cache/routing/flush' },
                    { label: 'Flush action cache', group: 'Action', keywords: 'cache flush action', action: 'api', method: 'POST', url: '/api/admin/cache/action/flush' },
                    { label: 'Refresh entity index', group: 'Action', keywords: 'entity index refresh', action: 'api', method: 'POST', url: '/api/admin/entity-index/refresh' },
                    { label: 'Reload custom agents', group: 'Action', keywords: 'custom agents reload', action: 'api', method: 'POST', url: '/api/admin/custom-agents/reload' },
                    { label: 'Logout', group: 'Action', keywords: 'logout sign out', action: 'logout' }
                );
                this.allCommands = cmds;
            },
            get results() {
                var q = (this.query || '').toLowerCase().trim();
                if (!q) return this.allCommands.slice(0, 10);
                var scored = this.allCommands.map(function (c) {
                    var text = (c.label + ' ' + (c.keywords || '') + ' ' + (c.group || '')).toLowerCase();
                    var score = 0;
                    if (c.label.toLowerCase().startsWith(q)) score += 10;
                    if (text.indexOf(q) !== -1) score += 5;
                    return { cmd: c, score: score };
                }).filter(function (s) { return s.score > 0; });
                scored.sort(function (a, b) { return b.score - a.score; });
                return scored.map(function (s) { return s.cmd; });
            },
            onKeydown(e) {
                if (!this.open) return;
                if (e.key === 'Escape') {
                    e.preventDefault();
                    this.open = false;
                    return;
                }
                if (!this.results.length) return;
                if (e.key === 'ArrowDown') {
                    e.preventDefault();
                    this.selectedIndex = Math.min(this.selectedIndex + 1, this.results.length - 1);
                } else if (e.key === 'ArrowUp') {
                    e.preventDefault();
                    this.selectedIndex = Math.max(this.selectedIndex - 1, 0);
                } else if (e.key === 'Enter') {
                    e.preventDefault();
                    var cmd = this.results[this.selectedIndex];
                    if (cmd) this.execute(cmd);
                }
            },
            execute(cmd) {
                if (cmd.action === 'navigate') {
                    window.location.assign(cmd.href);
                } else if (cmd.action === 'logout') {
                    var form = document.querySelector('.sidebar-logout-form');
                    if (form) form.submit();
                } else if (cmd.action === 'api') {
                    window.dashboardApi.request(cmd.url, { method: cmd.method || 'POST' })
                        .then(function () { window.toast(cmd.label + ' succeeded', 'success'); })
                        .catch(function (e) { window.toast(e.detail || cmd.label + ' failed', 'error'); });
                }
                this.open = false;
                this.query = '';
                this.selectedIndex = 0;
            }
        };
    };

    /* === dashLiveStream === */
    var dashLiveStream = function (url, opts) {
        opts = opts || {};
        return {
            _es: null,
            _reconnectCount: 0,
            _reconnectTimer: null,
            _pollTimer: null,
            _maxReconnect: 3,
            _backoffMs: 1000,
            start() {
                if (typeof EventSource === 'undefined') {
                    this._fallback();
                    return;
                }
                this._connect();
            },
            stop() {
                if (this._es) {
                    this._es.close();
                    this._es = null;
                }
                if (this._reconnectTimer) {
                    clearTimeout(this._reconnectTimer);
                    this._reconnectTimer = null;
                }
                if (this._pollTimer) {
                    clearInterval(this._pollTimer);
                    this._pollTimer = null;
                }
            },
            _connect() {
                var self = this;
                try {
                    this._es = new EventSource(window.dashUrl(url));
                    this._es.onopen = function () {
                        self._reconnectCount = 0;
                        if (opts.onOpen) opts.onOpen();
                    };
                    this._es.onmessage = function (e) {
                        try {
                            var data = JSON.parse(e.data);
                            if (opts.onMessage) opts.onMessage(data);
                        } catch (_) { }
                    };
                    this._es.onerror = function () {
                        self._es.close();
                        self._es = null;
                        self._reconnectCount++;
                        if (self._reconnectCount > self._maxReconnect) {
                            if (opts.onError) opts.onError();
                            self._fallback();
                            return;
                        }
                        var delay = Math.min(self._backoffMs * Math.pow(2, self._reconnectCount - 1), 30000);
                        self._reconnectTimer = setTimeout(function () { self._connect(); }, delay);
                    };
                } catch (_) {
                    this._fallback();
                }
            },
            _fallback() {
                var self = this;
                if (opts.fallbackPollMs && opts.onMessage) {
                    var doFetch = function () {
                        window.dashboardApi.json(url).then(function (data) {
                            if (opts.onMessage) opts.onMessage(data);
                        }).catch(function () { });
                    };
                    this._pollTimer = setInterval(doFetch, opts.fallbackPollMs);
                    doFetch();
                    if (opts.onFallback) opts.onFallback();
                }
            }
        };
    };

    /* === dashSettingsRail === */
    var dashSettingsRail = function (opts) {
        opts = opts || {};
        return {
            categories: opts.categories || [],
            activeCategory: opts.defaultCategory || '',
            search: '',
            init() {
                var hash = window.location.hash.replace('#', '');
                if (hash && this.categories.indexOf(hash) !== -1) {
                    this.activeCategory = hash;
                } else if (!this.activeCategory && this.categories.length) {
                    this.activeCategory = this.categories[0];
                }
            },
            setCategory(cat) {
                this.activeCategory = cat;
                window.location.hash = cat;
            }
        };
    };

    /* === dashModelCatalog ===
     * Client-side catalog of the models a configured LLM provider offers.
     * fetchModels() never throws: transport, HTTP, and auth failures resolve
     * to {status: 'error', ...}. Successful lists are memoized per provider for
     * the page load; failed lookups are not cached so the next call retries.
     */
    var MODEL_FALLBACKS = {
        openrouter: ['openai/gpt-4o-mini', 'openai/gpt-4o', 'anthropic/claude-sonnet-4.5', 'google/gemini-2.5-flash', 'meta-llama/llama-3.1-8b-instruct'],
        groq: ['llama-3.1-8b-instant', 'llama-3.3-70b-versatile', 'openai/gpt-oss-120b', 'openai/gpt-oss-20b'],
        anthropic: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-haiku-4-5'],
        cerebras: ['llama3.1-8b', 'llama-3.3-70b', 'gpt-oss-120b'],
        ollama: ['llama3.2', 'llama3.1', 'mistral', 'gemma2'],
        custom_openai: ['gpt-4o-mini', 'gpt-4o', 'deepseek-chat', 'kimi-latest']
    };
    var MODEL_PLACEHOLDERS = {
        openrouter: 'openai/gpt-4o-mini',
        groq: 'llama-3.1-8b-instant',
        anthropic: 'claude-haiku-4-5',
        cerebras: 'llama3.1-8b',
        ollama: 'llama3.2',
        custom_openai: 'gpt-4o-mini'
    };
    var modelCatalogCache = {};

    var normalizeModelList = function (provider, data) {
        if (!data || typeof data !== 'object') {
            return { provider: provider, status: 'error', error_kind: 'provider_error', detail: 'Invalid response', models: [] };
        }
        var models = [];
        (Array.isArray(data.models) ? data.models : []).forEach(function (m) {
            if (m && typeof m.id === 'string' && m.id) {
                models.push({
                    id: m.id,
                    name: typeof m.name === 'string' && m.name ? m.name : m.id,
                    model: typeof m.model === 'string' && m.model ? m.model : provider + '/' + m.id
                });
            }
        });
        if (data.status === 'ok') {
            return { provider: provider, status: 'ok', models: models, cached: !!data.cached, fetched_at: data.fetched_at || null };
        }
        return {
            provider: provider,
            status: 'error',
            error_kind: data.error_kind || 'provider_error',
            detail: data.detail || 'Model list unavailable',
            models: []
        };
    };

    var dashModelCatalog = {
        fetchModels: function (provider, opts) {
            opts = opts || {};
            if (!provider) {
                return Promise.resolve({ provider: provider, status: 'error', error_kind: 'not_configured', detail: 'No provider selected', models: [] });
            }
            if (!opts.refresh && modelCatalogCache[provider]) {
                return modelCatalogCache[provider];
            }
            var url = '/api/admin/llm-providers/' + encodeURIComponent(provider) + '/models' + (opts.refresh ? '?refresh=true' : '');
            var pending = Promise.resolve()
                .then(function () { return window.dashboardApi.json(url); })
                .then(function (data) {
                    return normalizeModelList(provider, data);
                }, function (e) {
                    var kind = e && e.code === 'auth_expired' ? 'auth' : 'unreachable';
                    return { provider: provider, status: 'error', error_kind: kind, detail: (e && e.detail) || 'Request failed', models: [] };
                })
                .then(function (result) {
                    if (result.status !== 'ok' && modelCatalogCache[provider] === pending) {
                        delete modelCatalogCache[provider];
                    }
                    return result;
                });
            modelCatalogCache[provider] = pending;
            return pending;
        },
        invalidate: function (provider) {
            if (provider) {
                delete modelCatalogCache[provider];
            } else {
                modelCatalogCache = {};
            }
        },
        fallbackModels: function (provider) {
            return (MODEL_FALLBACKS[provider] || []).map(function (id) {
                return { id: id, name: id, model: provider + '/' + id };
            });
        },
        placeholder: function (provider) {
            return MODEL_PLACEHOLDERS[provider] || 'model-name';
        }
    };

    /* === Register on window === */
    window.dashboardShell = dashboardShell;
    window.dashModal = dashModal;
    window.dashToasts = dashToasts;
    window.dashPage = dashPage;
    window.dashDataTable = dashDataTable;
    window.dashSidebarGroups = dashSidebarGroups;
    window.dashCommandPalette = dashCommandPalette;
    window.dashLiveStream = dashLiveStream;
    window.dashSettingsRail = dashSettingsRail;
    window.dashModelCatalog = dashModelCatalog;
})();
