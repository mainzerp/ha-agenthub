#!/usr/bin/env python3
"""Build the single-file Lovelace card shipped by the Home Assistant integration.

The Agent Activity Orb widget (``container/app/dashboard/static/activity-orb.js``) and its Lovelace card
wrapper (``container/app/dashboard/static/activity-orb-card.src.js``) are concatenated, behind a
generated-file banner with a version/build marker, into
``custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js``. The integration registers that one
file as a frontend resource.

Deferred definition (the "Custom element doesn't exist" fix): the Home Assistant frontend replaces
``window.customElements`` with the scoped-custom-element-registry polyfill when its app bundle boots, and the
polyfilled ``get()``/``whenDefined()`` only see tags defined through the polyfill. ``frontend.add_extra_js_url``
modules can run before that happens, so a native ``define`` made too early is invisible to Lovelace. The
generated file therefore wraps the whole body (both elements and ``customCards``) in ``run()`` and calls it:

* immediately, when the polyfill is already installed (``window.CustomElementRegistryPolyfill``) or the registry
  already knows ``home-assistant``;
* immediately, on any page without a ``<home-assistant>`` element (dashboard previews, test harnesses);
* otherwise (the HA index page on a cold load) as soon as the polyfill (or a replaced ``window.customElements``) is
  detected by short timers, or ``customElements.whenDefined('home-assistant')`` resolves, with a 30 s
  ``setTimeout`` fallback so the card is never lost (a late define is safe, HA rebuilds the card when
  ``whenDefined`` fires; an early one is the broken case).

Healing: whatever path defines the elements, ``heal()`` verifies on short timers for 60 s (and on
``whenDefined('home-assistant')``) that ``window.customElements.get(tag)`` still finds them, and defines them again
on the registry that replaced the original one.

Containment and diagnostics: each source part runs in its own ``try`` block (a failing widget cannot prevent the card
from being defined), errors are recorded in ``window.__AGENTHUB_ORB_DIAG__`` and logged, never thrown, and the gate
path, timestamps and healing events land in the same object. ``run()`` executes exactly once.

Usage:
    python scripts/sync_orb_card.py           # write/refresh the generated file
    python scripts/sync_orb_card.py --check   # exit 1 when the generated file is missing or stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

WIDGET_SRC = "container/app/dashboard/static/activity-orb.js"
CARD_SRC = "container/app/dashboard/static/activity-orb-card.src.js"
TARGET = "custom_components/ha_agenthub/frontend/agenthub-activity-orb-card.js"
VERSION_SRC = "container/app/__init__.py"
BUILD_SRC = "container/app/dashboard/static_assets.py"

FIX_COMMAND = "python scripts/sync_orb_card.py"


def _normalize(text: str) -> str:
    """Normalize line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_text(path: Path) -> str:
    return _normalize(path.read_bytes().decode("utf-8"))


def _find(pattern: str, text: str, what: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        raise SystemExit(f"sync_orb_card: cannot find {what}")
    return match.group(1)


def build_marker(root: Path = REPO_ROOT) -> str:
    """Version plus static build counter, the same string the dashboard uses for ``?v=`` cache busting."""
    version = _find(r'^__version__\s*=\s*"([^"]+)"', _read_text(root / VERSION_SRC), "__version__")
    build = _find(r'^_STATIC_BUILD\s*=\s*"([^"]+)"', _read_text(root / BUILD_SRC), "_STATIC_BUILD")
    return f"{version}.{build}"


FALLBACK_MS = 30000
WATCH_MS = 60000

# The concatenated sources go between these parts, inside run().
WRAPPER_HEAD = """(function () {
    'use strict';

    var FALLBACK_MS = __FALLBACK_MS__;
    var WATCH_MS = __WATCH_MS__;

    function nowMs() { try { return Math.round(performance.now()); } catch (_) { return 0; } }
    function info(text) { try { console.info('[agenthub-orb] ' + text); } catch (_) { /* ignore */ } }

    // Field diagnostics: window.__AGENTHUB_ORB_DIAG__ (plain object, see docs/style-guide.md).
    var D = window.__AGENTHUB_ORB_DIAG__;
    if (!D || typeof D !== 'object') { D = window.__AGENTHUB_ORB_DIAG__ = {}; }
    D.build = window.__AGENTHUB_ORB_BUILD__ || D.build || 'unknown';
    if (!Array.isArray(D.errors)) { D.errors = []; }
    if (!D.errorCounts || typeof D.errorCounts !== 'object') { D.errorCounts = {}; }
    if (!Array.isArray(D.defined)) { D.defined = []; }
    if (!Array.isArray(D.healing)) { D.healing = []; }
    if (!D.t || typeof D.t !== 'object') { D.t = {}; }
    if (!D.cards || typeof D.cards !== 'object') { D.cards = {}; }
    D.gatePath = 'pending';
    D.healed = false;
    D.t.evaluated = nowMs();
    D.evaluatedAt = new Date().toISOString();

    function fail(where, err) {
        try {
            var msg = err && err.message !== undefined ? String(err.message) : String(err);
            D.errors.push({ t: nowMs(), where: where, message: msg.slice(0, 300), stack: err && err.stack ? String(err.stack).split('\\n').slice(0, 4).join(' | ').slice(0, 400) : '' });
            if (D.errors.length > 20) { D.errors.shift(); }
            D.errorCounts[where + ': ' + msg] = (D.errorCounts[where + ': ' + msg] || 0) + 1;
            console.error('[agenthub-orb] ' + where + ': ' + msg, err);
        } catch (_) { /* ignore */ }
    }

    var registry0 = window.customElements;
    var done = false;
    var ours = [];            // [tag, constructor] of every element this evaluation defined
    var fallbackTimer = 0;
    var watchTimer = 0;
    var watchStart = nowMs();
    var attachedTo = null;    // registry whose whenDefined('home-assistant') we already listen to
    var healFails = {};

    function note(kind, tag, reason, message) {
        D.healing.push({ t: nowMs(), kind: kind, tag: tag || null, reason: reason || null, message: message || null });
        if (D.healing.length > 30) { D.healing.shift(); }
    }

    // Defines an element on the current registry and remembers it so that it can be registered again when the
    // HA frontend replaces window.customElements afterwards. Never throws.
    function __orbDefine(tag, ctor) {
        try {
            window.customElements.define(tag, ctor);
            ours.push([tag, ctor]);
            D.defined.push(tag);
            D.t['defined:' + tag] = nowMs();
            D.polyfillAtDefine = !!window.CustomElementRegistryPolyfill;
            info('defined ' + tag + ' (build ' + D.build + ', gate ' + D.gatePath + ', t=' + nowMs() + ' ms, polyfill ' + (window.CustomElementRegistryPolyfill ? 'present' : 'absent') + ')');
        } catch (e) {
            fail('define ' + tag, e);
        }
    }

    // The scoped-custom-element-registry polyfill (installed by the HA frontend) only sees tags defined through
    // it: when it replaced window.customElements after our define, define the same classes on the new registry.
    function heal(reason) {
        var reg = window.customElements;
        if (!reg || !ours.length) { return; }
        for (var i = 0; i < ours.length; i++) {
            var tag = ours[i][0], ctor = ours[i][1], have;
            if ((healFails[tag] || 0) >= 3) { continue; }
            try { have = reg.get(tag); } catch (_) { have = undefined; }
            if (have) { continue; }
            try {
                reg.define(tag, ctor);
                D.healed = true;
                note('healed', tag, reason);
                info('registered ' + tag + ' again on the replaced registry (' + reason + ', t=' + nowMs() + ' ms)');
            } catch (e) {
                healFails[tag] = (healFails[tag] || 0) + 1;
                note('heal-failed', tag, reason, e && e.message);
                if (healFails[tag] === 1) { fail('heal ' + tag, e); }
            }
        }
        if (reg !== attachedTo && typeof reg.whenDefined === 'function') {
            attachedTo = reg;
            try {
                reg.whenDefined('home-assistant').then(function () { heal('home-assistant-defined'); }, function () { /* ignore */ });
            } catch (_) { /* ignore */ }
        }
    }

    // Short timers up to WATCH_MS: detect the polyfill (or a replaced registry) before the define, and verify
    // afterwards that the tags are still visible through the registry Lovelace uses.
    function watch() {
        watchTimer = 0;
        var elapsed = nowMs() - watchStart;
        try {
            if (!done && (window.CustomElementRegistryPolyfill || window.customElements !== registry0)) { run('polyfill-detected'); }
            if (done) { heal('watch'); }
        } catch (e) { fail('gate watch', e); }
        if (elapsed < WATCH_MS) { watchTimer = setTimeout(watch, elapsed < 10000 ? 100 : 1000); }
    }

    function run(path) {
        if (done) { return; }
        done = true;
        if (fallbackTimer) { clearTimeout(fallbackTimer); fallbackTimer = 0; }
        D.gatePath = path;
        D.t.run = nowMs();
        info('module ' + D.build + ' running (gate ' + path + ', t=' + nowMs() + ' ms)');
"""

WRAPPER_PART_OPEN = "        try {\n"
WRAPPER_PART_CLOSE = "\n        } catch (err) { fail('__PART__', err); }\n"

WRAPPER_TAIL = """        try { heal('after-define'); } catch (_) { /* ignore */ }
        if (!watchTimer && nowMs() - watchStart < WATCH_MS) { watchTimer = setTimeout(watch, 100); }
    }

    // Define the elements only once the HA frontend replaced window.customElements with its scoped registry
    // polyfill; a native define made earlier is invisible to customElements.get()/whenDefined() in Lovelace.
    // Pages without a <home-assistant> element (dashboard previews, test harnesses) never wait. A late define is
    // safe (HA rebuilds the card when whenDefined fires), an early one is the broken case, so the fallback is long.
    // Whatever path defines the elements, heal() registers them again if the registry is replaced afterwards.
    info('module ' + D.build + ' evaluated (t=' + D.t.evaluated + ' ms)');
    try {
        var registry = window.customElements;
        var path = '';
        if (window.CustomElementRegistryPolyfill) { path = 'immediate-polyfill'; }
        else if (!document.querySelector('home-assistant')) { path = 'immediate-no-ha-root'; }
        else if (!registry || typeof registry.whenDefined !== 'function') { path = 'immediate-unsupported'; }
        else if (registry.get('home-assistant')) { path = 'immediate-registry-ready'; }
        if (path) {
            run(path);
            return;
        }
        registry.whenDefined('home-assistant').then(function () { run('whenDefined'); }, function () { /* ignore */ });
        fallbackTimer = setTimeout(function () { run('fallback'); }, FALLBACK_MS);
        watchTimer = setTimeout(watch, 100);
    } catch (e) {
        fail('gate', e);
        try { run('error-fallback'); } catch (_) { /* ignore */ }
    }
})();
"""


def render(root: Path = REPO_ROOT) -> str:
    """Return the expected content of the generated card file."""
    marker = build_marker(root)
    banner = (
        "/* GENERATED by scripts/sync_orb_card.py - do not edit.\n"
        f" * Sources: {WIDGET_SRC} + {CARD_SRC}\n"
        f" * Build: {marker}\n"
        f" * Regenerate with: {FIX_COMMAND}\n"
        " * The body runs inside run(), deferred until the HA frontend has booted (see the sync script).\n"
        " */\n"
        f'window.__AGENTHUB_ORB_BUILD__ = "{marker}";\n'
    )
    widget = _read_text(root / WIDGET_SRC).rstrip("\n") + "\n"
    card = _read_text(root / CARD_SRC).rstrip("\n") + "\n"
    head = WRAPPER_HEAD.replace("__FALLBACK_MS__", str(FALLBACK_MS)).replace("__WATCH_MS__", str(WATCH_MS))
    # Each source part runs in its own try block: a failing widget must not prevent the card from being defined.
    parts = ""
    for name, text in (("widget", widget), ("card", card)):
        parts += WRAPPER_PART_OPEN + text + WRAPPER_PART_CLOSE.replace("__PART__", name)
    return banner + "\n" + head + parts + WRAPPER_TAIL


def find_stale(root: Path = REPO_ROOT) -> list[str]:
    """Return a description of every problem with the generated file (empty when it is current)."""
    for rel in (WIDGET_SRC, CARD_SRC):
        if not (root / rel).is_file():
            return [f"{rel}: source file missing"]
    target = root / TARGET
    if not target.is_file():
        return [f"{TARGET}: missing"]
    if _read_text(target) != render(root):
        return [f"{TARGET}: stale"]
    return []


def sync(root: Path = REPO_ROOT) -> bool:
    """Write the generated file; return True when it changed."""
    target = root / TARGET
    expected = render(root)
    if target.is_file() and _read_text(target) == expected:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(expected.encode("utf-8"))
    return True


def main(argv: list[str] | None = None, root: Path = REPO_ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify the generated file instead of writing it")
    args = parser.parse_args(argv)

    if args.check:
        problems = find_stale(root)
        if problems:
            print(f"Generated Lovelace card is out of date (run: {FIX_COMMAND}):")
            for problem in problems:
                print(f"  - {problem}")
            return 1
        print(f"Generated Lovelace card is up to date ({TARGET}).")
        return 0

    if sync(root):
        print(f"updated {TARGET}")
    else:
        print("Generated Lovelace card already up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
