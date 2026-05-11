/* === HA-AgentHub Dashboard Shared Components === */

    __dashboard = __dashboard || {};
    __dashboard.rootPath = (__dashboard && __dashboard.rootPath) || '';
    __dashboard.loginUrl = (__dashboard && __dashboard.loginUrl) || '/dashboard/login';

    var dashUrl = function(url) {
        if (!url || typeof url !== 'string' || url.charAt(0) !== '/') {
            return url;
        }
        var rootPath = (__dashboard && __dashboard.rootPath) || '';
        if (!rootPath) {
            return url;
        }
        return rootPath + url;
    };

    function getErrorDetail(payload, fallback) {
        if (!payload) {
            return fallback;
        }
        if (typeof payload.detail === 'string' && payload.detail) {
            return payload.detail;
        }
        if (Array.isArray(payload.detail) && payload.detail.length && payload.detail[0] && payload.detail[0].msg) {
            return payload.detail[0].msg;
        }
        if (typeof payload.message === 'string' && payload.message) {
            return payload.message;
        }
        return fallback;
    }

    function buildError(message, response, payload) {
        var error = new Error(message);
        error.status = response ? response.status : null;
        error.detail = message;
        error.payload = payload || null;
        return error;
    }

    var dashboardApi = {
        loginUrl: __dashboard.loginUrl,

        redirectToLogin: function(redirectUrl) {
            window.location.assign(redirectUrl || this.loginUrl);
        },

        async request(url, options) {
            var requestUrl = typeof url === 'string' && url.charAt(0) === '/' ? dashUrl(url) : url;
            var response = await fetch(requestUrl, Object.assign({ credentials: 'same-origin' }, options || {}));
            var redirectUrl = response.headers.get('HX-Redirect');

            if (response.status === 401 || redirectUrl) {
                var authError = buildError('Session expired', response, null);
                authError.code = 'auth_expired';
                authError.redirectTo = redirectUrl || this.loginUrl;
                this.redirectToLogin(authError.redirectTo);
                throw authError;
            }

            if (!response.ok) {
                var payload = null;
                var fallback = response.statusText || ('Request failed (' + response.status + ')');

                try {
                    payload = await response.clone().json();
                } catch (_) {
                    payload = null;
                }

                if (payload) {
                    throw buildError(getErrorDetail(payload, fallback), response, payload);
                }

                try {
                    var text = await response.text();
                    if (text) {
                        fallback = text;
                    }
                } catch (_) {
                    // Keep the original fallback.
                }

                throw buildError(fallback, response, null);
            }

            return response;
        },

        async json(url, options) {
            var response = await this.request(url, options);
            return await response.json();
        }
    };

    var dashFetch = function(url, options) {
        return dashboardApi.request(url, options);
    };

    dashFetch.json = function(url, options) {
        return dashboardApi.json(url, options);
    };

    var dashboardShell = function() {
        return {
            sidebarOpen: true,
            isMobile: false,
            init() {
                var mql = window.matchMedia('(max-width: 768px)');
                var apply = function() {
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
            },
            get sidebarInert() {
                return this.isMobile && !this.sidebarOpen;
            }
        };
    };

    var dashModal = function(initialOpen) {
        return {
            open: !!initialOpen,
            _previouslyFocused: null,
            show() {
                this._previouslyFocused = document.activeElement;
                this.open = true;
                this.$nextTick(function() {
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

    var dashToasts = function() {
        return {
            toasts: [],
            push(message, kind) {
                var id = Date.now() + Math.random();
                this.toasts.push({ id: id, message: message, kind: kind });
                setTimeout(function() {
                    this.toasts = this.toasts.filter(function(t) { return t.id !== id; });
                }.bind(this), 4000);
            }
        };
    };

    var toast = function(msg, kind) {
        var root = document.getElementById('toast-root');
        if (root && root.__x) {
            root.__x.$data.push(msg, kind);
        }
    };

    var chartColors = function() {
        var root = getComputedStyle(document.documentElement);
        return {
            teal: root.getPropertyValue('--teal').trim(),
            sage: root.getPropertyValue('--sage').trim(),
            coral: root.getPropertyValue('--coral').trim(),
            amber: root.getPropertyValue('--amber').trim(),
            lavender: root.getPropertyValue('--lavender').trim(),
            blue: root.getPropertyValue('--blue').trim(),
            purple: root.getPropertyValue('--purple').trim(),
            text: root.getPropertyValue('--text-primary').trim()
        };
    };

    var chartRgba = function(token, alpha) {
        var colors = chartColors();
        var hex = colors[token] || token;
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        return 'rgba(' + r + ', ' + g + ', ' + b + ', ' + alpha + ')';
    };

    /* === dashPage === */
    var dashPage = function(opts) {
        opts = opts || {};
        return {
            _pollTimers: [],
            init() {
                if (opts.mount) {
                    opts.mount.call(this);
                }
            },
            destroy() {
                this._pollTimers.forEach(function(t) { clearInterval(t); });
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
    var dashDataTable = function(opts) {
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
                    rows = rows.filter(function(r) {
                        return Object.values(r).some(function(v) {
                            return String(v).toLowerCase().indexOf(f) !== -1;
                        });
                    });
                }
                if (this.sortKey) {
                    var k = this.sortKey;
                    var asc = this.sortAsc;
                    rows.sort(function(a, b) {
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
    var dashSidebarGroups = function(initialState) {
        var storageKey = 'dash.sidebar.groups';
        var saved = {};
        try {
            saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
        } catch (_) {}
        return {
            groups: Object.assign({}, initialState || {}, saved),
            toggle(key) {
                this.groups[key] = !this.groups[key];
                try {
                    localStorage.setItem(storageKey, JSON.stringify(this.groups));
                } catch (_) {}
            },
            isOpen(key) {
                return !!this.groups[key];
            }
        };
    };

    /* === dashCommandPalette (Phase 5) === */
    var dashCommandPalette = function() {
        return {
            open: false,
            query: '',
            selectedIndex: 0,
            allCommands: [],
            pageCommands: [],
            init() {
                var self = this;
                this.buildCommands();
                document.addEventListener('keydown', function(e) {
                    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
                        e.preventDefault();
                        self.open = true;
                        self.$nextTick(function() {
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
                // Nav items from JSON payload
                var navData = document.getElementById('dash-nav-data');
                if (navData) {
                    try {
                        var groups = JSON.parse(navData.textContent);
                        groups.forEach(function(g) {
                            g.items.forEach(function(item) {
                                var itemHref = item.href || ('/dashboard/' + item.href_name.replace('_page', '').replace(/_/g, '-'));
                                cmds.push({
                                    label: item.label,
                                    group: g.label,
                                    keywords: item.label + ' ' + g.label,
                                    action: 'navigate',
                                    href: dashUrl(itemHref)
                                });
                            });
                        });
                    } catch (_) {}
                }
                // Static actions
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
                var scored = this.allCommands.map(function(c) {
                    var text = (c.label + ' ' + (c.keywords || '') + ' ' + (c.group || '')).toLowerCase();
                    var score = 0;
                    if (c.label.toLowerCase().startsWith(q)) score += 10;
                    if (text.indexOf(q) !== -1) score += 5;
                    return { cmd: c, score: score };
                }).filter(function(s) { return s.score > 0; });
                scored.sort(function(a, b) { return b.score - a.score; });
                return scored.map(function(s) { return s.cmd; });
            },
            onKeydown(e) {
                if (!this.open) return;
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
                } else if (e.key === 'Escape') {
                    e.preventDefault();
                    this.open = false;
                }
            },
            execute(cmd) {
                if (cmd.action === 'navigate') {
                    window.location.assign(cmd.href);
                } else if (cmd.action === 'logout') {
                    var form = document.querySelector('.sidebar-logout-form');
                    if (form) form.submit();
                } else if (cmd.action === 'api') {
                    dashboardApi.request(cmd.url, { method: cmd.method || 'POST' })
                        .then(function() { toast(cmd.label + ' succeeded', 'success'); })
                        .catch(function(e) { toast(e.detail || cmd.label + ' failed', 'error'); });
                }
                this.open = false;
                this.query = '';
                this.selectedIndex = 0;
            }
        };
    };

    /* === dashLiveStream (stub -> Phase 6) === */
    var dashLiveStream = function(url, opts) {
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
                    this._es = new EventSource(dashUrl(url));
                    this._es.onopen = function() {
                        self._reconnectCount = 0;
                        if (opts.onOpen) opts.onOpen();
                    };
                    this._es.onmessage = function(e) {
                        try {
                            var data = JSON.parse(e.data);
                            if (opts.onMessage) opts.onMessage(data);
                        } catch (_) {}
                    };
                    this._es.onerror = function() {
                        self._es.close();
                        self._es = null;
                        self._reconnectCount++;
                        if (self._reconnectCount > self._maxReconnect) {
                            if (opts.onError) opts.onError();
                            self._fallback();
                            return;
                        }
                        var delay = Math.min(self._backoffMs * Math.pow(2, self._reconnectCount - 1), 30000);
                        self._reconnectTimer = setTimeout(function() { self._connect(); }, delay);
                    };
                } catch (_) {
                    this._fallback();
                }
            },
            _fallback() {
                if (opts.fallbackPollMs && opts.onMessage) {
                    this._pollTimer = setInterval(function() {
                        if (opts.onMessage) opts.onMessage();
                    }, opts.fallbackPollMs);
                    if (opts.onFallback) opts.onFallback();
                }
            }
        };
    };

    /* === Format helpers === */
    var dashFormatRelativeTime = function(ts) {
        if (!ts) return '-';
        var now = Date.now();
        var then = new Date(ts).getTime();
        var diffS = Math.floor((now - then) / 1000);
        if (diffS < 60) return diffS + 's ago';
        if (diffS < 3600) return Math.floor(diffS / 60) + 'm ago';
        if (diffS < 86400) return Math.floor(diffS / 3600) + 'h ago';
        return Math.floor(diffS / 86400) + 'd ago';
    };

    var dashFormatBytes = function(n) {
        if (n === 0) return '0 B';
        var k = 1024;
        var sizes = ['B', 'KB', 'MB', 'GB'];
        var i = Math.floor(Math.log(n) / Math.log(k));
        return parseFloat((n / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    };

    var _agentColorClasses = {
        'orchestrator': 'purple',
        'light-agent': 'yellow',
        'music-agent': 'blue',
        'climate-agent': 'green',
        'timer-agent': 'red',
        'media-agent': 'pink',
        'scene-agent': 'indigo',
        'automation-agent': 'teal',
        'security-agent': 'orange',
        'general-agent': 'muted',
        'multi-agent': 'purple',
        'user': 'muted',
        'rewrite-agent': 'orange',
    };

    var _agentColorPalette = ['purple', 'yellow', 'blue', 'green', 'red', 'pink', 'indigo', 'teal', 'orange'];

    var _agentClassToHex = {
        'purple': '#8b5cf6',
        'yellow': '#f59e0b',
        'blue': '#3b82f6',
        'green': '#10b981',
        'red': '#ef4444',
        'pink': '#ec4899',
        'indigo': '#6366f1',
        'teal': '#14b8a6',
        'orange': '#f97316',
        'muted': '#6b7280',
    };

    var _traceSpanColors = {
        'cache_lookup': '#06b6d4',
        'classify': '#8b5cf6',
        'dispatch': '#3b82f6',
        'dispatch_content': '#2563eb',
        'dispatch_send': '#1d4ed8',
        'entity_match': '#a855f7',
        'filler_generate': '#4ade80',
        'filler_send': '#22c55e',
        'llm_call': '#f59e0b',
        'ha_action': '#10b981',
        'return': '#ec4899',
        'rewrite': '#f97316',
        'mediation': '#fb923c',
        'mcp_tool_call': '#14b8a6',
        'ha_call': '#059669',
        'llm_provider_call': '#d97706',
        'cache_fallthrough': '#f43f5e',
    };

    var dashTruncate = function(s, n) {
        if (!s || s.length <= n) return s;
        return s.substring(0, n) + '...';
    };

    /* === Settings helpers (Phase 4) === */
    var dashSettingsRail = function(opts) {
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

    var dashSettingsSearch = function(rootSelector) {
        return {
            search: '',
            apply() {
                var root = document.querySelector(rootSelector);
                if (!root) return;
                var sections = root.querySelectorAll('[data-category]');
                var term = this.search.toLowerCase().trim();
                sections.forEach(function(sec) {
                    var rows = sec.querySelectorAll('.form-group, .provider-row, .ha-config, [data-setting]');
                    var sectionMatch = false;
                    rows.forEach(function(row) {
                        var text = row.textContent.toLowerCase();
                        var match = !term || text.indexOf(term) !== -1;
                        row.style.display = match ? '' : 'none';
                        if (match) sectionMatch = true;
                    });
                    sec.style.display = sectionMatch ? '' : 'none';
                });
            }
        };
    };
