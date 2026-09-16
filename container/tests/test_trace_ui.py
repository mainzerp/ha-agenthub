"""Behavior checks for trace filtering and elapsed-time inspection."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).parents[1] / "app/dashboard/templates"


def run_page_script(template: str, assertions: str) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for dashboard JavaScript checks")
    text = (TEMPLATES / template).read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", text, re.S).group(1)
    script = script.replace("{{ trace_id | tojson }}", '"trace-fixture"')
    result = subprocess.run(
        [node, "-e", script + "\n" + assertions],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_trace_filter_reset_export_and_latest_request():
    run_page_script(
        "traces.html",
        r"""
        const assert = require('node:assert/strict');
        (async () => {
            const calls = [];
            global.window = {
                location: {search: '?search=session%201'},
                dashboardApi: {safeJson: async url => { calls.push(url); return {traces: [{trace_id:'a'}], total:1, pages:3}; }},
                dashUrl: url => '/prefix' + url,
                open: url => calls.push(url),
            };
            const page = tracesListPage();
            await page.init();
            assert.equal(page.search, 'session 1');
            assert.ok(calls[0].includes('search=session+1'));
            page.page = 2; page.agentFilter = 'light'; page.labelFilter = 'review'; page.dateFrom = '2026-01-01'; page.dateTo = '2026-02-01';
            await page.loadTraces();
            const query = new URLSearchParams(calls.at(-1).split('?')[1]);
            assert.equal(query.get('page'), '2'); assert.equal(query.get('agent'), 'light');
            page.exportCSV(); assert.ok(calls.at(-1).startsWith('/prefix/api/admin/traces/export?'));
            await page.resetFilters(); assert.equal(page.page, 1); assert.equal(page.hasFilters, false);
            const pending = [];
            window.dashboardApi.safeJson = () => new Promise(resolve => pending.push(resolve));
            const first = page.loadTraces(); const second = page.loadTraces();
            assert.equal(page.traces[0].trace_id, 'a'); assert.equal(page.loading, true);
            pending[1]({traces:[{trace_id:'latest'}]}); await second;
            pending[0]({traces:[{trace_id:'stale'}]}); await first;
            assert.equal(page.traces[0].trace_id, 'latest');
            window.dashboardApi.safeJson = async () => null;
            await page.loadTraces(); assert.equal(page.loadError, true); assert.equal(page.loading, false);
            assert.equal(page.traces[0].trace_id, 'latest');
        })().catch(e => { console.error(e); process.exitCode = 1; });
        """,
    )


def test_waterfall_hierarchy_parallel_offsets_and_missing_timing():
    run_page_script(
        "trace_detail.html",
        r"""
        const assert = require('node:assert/strict');
        const page = traceDetailPage();
        const span = (id, parent, start, duration) => ({metadata:{span_id:id}, parent_span:parent, start_time:start, duration_ms:duration, span_name:id});
        page.traceDetail = {spans:[
            span('root',null,1000,100), span('a','root',1020,30), span('b','root',1020,50),
            span('orphan','missing',null,null), span('cycle1','cycle2',null,0), span('cycle2','cycle1','bad',null)
        ]};
        page.renderGantt();
        assert.equal(page.ganttSpans.length, 6);
        const a = page.ganttSpans.find(r=>r.span.span_name==='a');
        const b = page.ganttSpans.find(r=>r.span.span_name==='b');
        assert.equal(a.depth,1); assert.equal(a.offset,20); assert.equal(b.offset,20);
        assert.equal(page.timelineDuration,100);
        for (const row of page.ganttSpans) assert.ok(!/NaN|Infinity/.test(page.barStyle(row)));
        assert.equal(page.formatDuration(0),'0.0 ms'); assert.equal(page.formatDuration(null),'Not recorded');
        page.zoom=8; page.$refs={waterfall:{scrollLeft:100}}; page.fitTimeline();
        assert.equal(page.zoom,1); assert.equal(page.$refs.waterfall.scrollLeft,0);
        page.traceDetail={spans:[]}; page.renderGantt(); assert.equal(page.timelineDuration,1);
        """,
    )


def test_inspector_preserves_all_metadata_and_neutral_missing_context():
    run_page_script(
        "trace_detail.html",
        r"""
        const assert = require('node:assert/strict');
        const page = traceDetailPage();
        const metadata = {latency_ms:0, ttft_ms:0, user_input:'<script>alert(1)</script>\nlong text', has_state_context:false, matches_attached:false, unknown_field:{nested:1}, error_type:'RecordedError'};
        page.selectedSpan={metadata};
        const groups=['overview','payload','metadata'].flatMap(group=>page.spanFields(group));
        assert.equal(groups.length,Object.keys(metadata).length);
        assert.equal(new Set(groups.map(([key])=>key)).size,groups.length);
        assert.equal(page.formatValue(false),'No');
        assert.equal(page.formatValue(metadata.user_input),metadata.user_input);
        page.traceDetail={}; assert.equal(page.routingSource(),'Not recorded');
        page.traceDetail={routing_cached:false}; assert.equal(page.routingSource(),'LLM');
        """,
    )
