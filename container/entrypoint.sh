#!/bin/sh
# Fix ownership of volume data (may be root-owned from previous images, from
# the build-time model download, or from a bind mount). The recursive chown
# only runs when the top two levels show a foreign owner, so restarts with a
# large Hugging Face cache stay fast.
if [ -n "$(find /data -maxdepth 2 \( ! -user app -o ! -group app \) -print -quit 2>/dev/null)" ]; then
    chown -R app:app /data 2>/dev/null || true
fi
exec setpriv --reuid=app --regid=app --init-groups --inh-caps=-all "$@"
