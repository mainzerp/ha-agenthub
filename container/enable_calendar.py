import sqlite3
conn = sqlite3.connect("/data/agent_assist.db")
c = conn.cursor()
c.execute("UPDATE agent_configs SET enabled = 1 WHERE agent_id = ?", ("calendar-agent",))
conn.commit()
c.execute("SELECT agent_id, enabled FROM agent_configs WHERE agent_id = ?", ("calendar-agent",))
print(c.fetchone())
conn.close()
