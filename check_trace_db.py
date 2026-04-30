import sqlite3
import json

conn = sqlite3.connect('container/data/agent_assist.db')
cur = conn.cursor()
cur.execute("SELECT trace_id, span_name, metadata FROM trace_spans WHERE agent_id = ? ORDER BY id DESC LIMIT 1", ("general-agent",))
row = cur.fetchone()
if row:
    trace_id, span_name, metadata = row
    print("Trace ID:", trace_id)
    print("Span:", span_name)
    data = json.loads(metadata) if metadata else {}
    response = data.get("response", "")
    print("Response length:", len(response))
    print("Response:", repr(response[:300]) if len(response) > 300 else repr(response))
else:
    print("No general-agent span found")
