#!/bin/sh
# Fix ownership of volume data (may be root-owned from previous images)
chown -R app:app /data 2>/dev/null || true
exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all "$@"
