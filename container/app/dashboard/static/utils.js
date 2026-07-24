(function () {
    'use strict';

    /* === Color helpers === */

    var chartColors = function () {
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

    var chartRgba = function (token, alpha) {
        var colors = chartColors();
        var hex = colors[token] || token;
        var r = parseInt(hex.slice(1, 3), 16);
        var g = parseInt(hex.slice(3, 5), 16);
        var b = parseInt(hex.slice(5, 7), 16);
        return 'rgba(' + r + ', ' + g + ', ' + b + ', ' + alpha + ')';
    };

    function dashChartLegendPosition() {
        return window.innerWidth <= 480 ? 'bottom' : 'right';
    }

    /* === Agent color maps === */

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

    var dashAgentClass = function (id) {
        if (!id) return 'muted';
        if (_agentColorClasses[id]) return _agentColorClasses[id];
        var hash = 0;
        for (var i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) | 0;
        return _agentColorPalette[Math.abs(hash) % _agentColorPalette.length];
    };

    var _agentClassToHex = {
        'purple': '#b98fa8',
        'yellow': '#f0b25c',
        'blue': '#8fb4cc',
        'green': '#7dab8c',
        'red': '#d4756b',
        'pink': '#d98a9a',
        'indigo': '#9a8fb8',
        'teal': '#e2a84b',
        'orange': '#d98a5c',
        'muted': '#8a8580',
    };

    var _traceSpanColors = {
        'cache_lookup': '#6a9ea6',
        'classify': '#9a8fb8',
        'dispatch': '#8fb4cc',
        'dispatch_content': '#7498b3',
        'dispatch_send': '#5a7c96',
        'entity_match': '#b98fa8',
        'filler_generate': '#8fbf9b',
        'filler_send': '#6fa87e',
        'llm_call': '#e2a84b',
        'ha_action': '#7dab8c',
        'return': '#d98a9a',
        'rewrite': '#d98a5c',
        'mediation': '#e0a374',
        'mcp_tool_call': '#f0b25c',
        'ha_call': '#5e8f70',
        'llm_provider_call': '#c98f38',
        'cache_fallthrough': '#d4756b',
    };

    /* === Format helpers === */

    var dashFormatBytes = function (n) {
        if (n === 0) return '0 B';
        var k = 1024;
        var sizes = ['B', 'KB', 'MB', 'GB'];
        var i = Math.floor(Math.log(n) / Math.log(k));
        return parseFloat((n / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    };

    var dashFormatTimestamp = function (ts) {
        if (!ts) return '-';
        try {
            var clean = ts.trim();
            if (!clean.match(/[Zz]|[+-]\d{2}:\d{2}$/)) {
                clean = clean.replace(' ', 'T') + 'Z';
            }
            var d = new Date(clean);
            return d.toLocaleString();
        } catch (_) { return ts; }
    };

    var dashFormatRelativeTime = function (ts) {
        if (!ts) return '-';
        var now = Date.now();
        var then = new Date(ts).getTime();
        var diffS = Math.floor((now - then) / 1000);
        if (diffS < 60) return diffS + 's ago';
        if (diffS < 3600) return Math.floor(diffS / 60) + 'm ago';
        if (diffS < 86400) return Math.floor(diffS / 3600) + 'h ago';
        return Math.floor(diffS / 86400) + 'd ago';
    };

    var dashTruncate = function (s, n) {
        if (!s || s.length <= n) return s;
        return s.substring(0, n) + '...';
    };

    var dashParseJsonSafe = function (str) {
        if (!str) return null;
        try {
            return JSON.parse(str);
        } catch (_) {
            return null;
        }
    };

    var dashChartOptions = function () {
        return {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { beginAtZero: true, grid: { color: chartRgba('text', 0.05) }, ticks: { color: chartColors().text } },
                x: { grid: { color: chartRgba('text', 0.05) }, ticks: { color: chartColors().text } }
            },
            plugins: {
                legend: { labels: { color: chartColors().text } }
            }
        };
    };

    var dashStatusClass = function (comp) {
        if (!comp) return 'status-unknown';
        return comp.status === 'healthy' ? 'status-healthy' : (comp.status === 'error' ? 'status-error' : 'status-unknown');
    };

    var dashBadgeClass = function (comp) {
        if (!comp) return '';
        if (comp.status === 'healthy') return 'health-badge--healthy';
        if (comp.status === 'error') return 'health-badge--error';
        if (comp.status === 'warning') return 'health-badge--warning';
        return '';
    };

    /* === Toast === */

    var toast = function (msg, kind) {
        var root = document.getElementById('toast-root');
        if (root && root.__x) {
            root.__x.$data.push(msg, kind);
        }
    };

    /* === Register on window === */

    window._agentColorClasses = _agentColorClasses;
    window._agentColorPalette = _agentColorPalette;
    window._agentClassToHex = _agentClassToHex;
    window._traceSpanColors = _traceSpanColors;
    window.chartColors = chartColors;
    window.chartRgba = chartRgba;
    window.dashChartLegendPosition = dashChartLegendPosition;
    window.dashFormatBytes = dashFormatBytes;
    window.dashFormatTimestamp = dashFormatTimestamp;
    window.dashFormatRelativeTime = dashFormatRelativeTime;
    window.dashTruncate = dashTruncate;
    window.dashParseJsonSafe = dashParseJsonSafe;
    window.dashChartOptions = dashChartOptions;
    window.dashStatusClass = dashStatusClass;
    window.dashBadgeClass = dashBadgeClass;
    window.dashAgentClass = dashAgentClass;
    window.toast = toast;
})();
