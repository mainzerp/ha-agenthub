---
name: agenthub-csrf
description: Log in to the HA-AgentHub dashboard and obtain the CSRF token + session cookie. Use before any Admin API call or dashboard form POST — the plain username/password login fails with 401 without a CSRF token.
---

# HA-AgentHub Login & CSRF Token

HA-AgentHub protects dashboard form POSTs with a double-submit CSRF token (cookie `agent_assist_csrf` + form field `csrf_token`, enforced by `verify_csrf` in `container/app/security/auth.py`). A bare `POST /dashboard/login` with only username/password returns **401 CSRF token missing**.

The token is minted by `GET /dashboard/login` and delivered two ways:
- as the `agent_assist_csrf` cookie (not HttpOnly)
- as a hidden form field `<input type="hidden" name="csrf_token" value="...">` in the HTML

## Login flow (session cookie + CSRF)

Credentials and the live URL come from `secrets/.env.local` (`AA_BASE_URL`, `AA_USERNAME`, `AA_PASSWORD`):

```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

# 1. Fetch the login page -> sets the CSRF cookie and renders the token
curl -s -c /tmp/aa_cookies.txt "$BASE/dashboard/login" \
  -o /tmp/aa_login.html --max-time 10

# 2. Extract the CSRF token from the hidden form field
CSRF=$(grep -o 'name="csrf_token" value="[^"]*"' /tmp/aa_login.html \
  | sed 's/.*value="//; s/"$//')

if [ -z "$CSRF" ]; then
  echo "ERROR: no csrf_token in login page" >&2
  exit 1
fi

# 3. Log in with the token; the session cookie is written to the same jar
curl -s -b /tmp/aa_cookies.txt -c /tmp/aa_cookies.txt \
  -X POST "$BASE/dashboard/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode username="$AA_USERNAME" \
  --data-urlencode password="$AA_PASSWORD" \
  --data-urlencode csrf_token="$CSRF" \
  --max-time 10
```

A successful login answers with `303` redirect to `/dashboard/` and sets the `agent_assist_session` cookie. After that, all examples in the other skills work with `$BASE` and `-b /tmp/aa_cookies.txt`.

## Using the token after login

- **JSON Admin API** (`/api/admin/...`): session cookie is enough — no CSRF field needed.
- **Dashboard form POSTs** (`/dashboard/...` with `application/x-www-form-urlencoded`): include the CSRF token as a form field. Read the current value from the cookie jar:

```bash
CSRF=$(grep agent_assist_csrf /tmp/aa_cookies.txt | awk '{print $NF}')

curl -s -b /tmp/aa_cookies.txt -c /tmp/aa_cookies.txt \
  -X POST "$BASE/dashboard/<some-form-endpoint>" \
  --data-urlencode csrf_token="$CSRF" \
  --data-urlencode other_field="..." \
  --max-time 10
```

## Token lifecycle

- Max age: 86400 s (24 h), same as the session.
- The token is bound to the session: after login, `ensure_csrf_token` keeps the existing cookie value, so the pre-login token stays valid for the session.
- `POST /dashboard/logout` deletes both cookies — re-run the login flow afterwards.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `401 CSRF token missing` | No `csrf_token` form field sent | Run the full login flow above |
| `401 CSRF token invalid` | Form field != cookie value (stale jar) | Re-fetch `/dashboard/login`, extract fresh token |
| `401 Invalid credentials` | Wrong `AA_USERNAME`/`AA_PASSWORD` | Check `secrets/.env.local` |
| Login page HTML has no token | Endpoint not reachable / setup mode | Verify `$BASE` and that onboarding is complete |
