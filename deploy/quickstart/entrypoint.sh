#!/usr/bin/env bash
# Quick start entrypoint. Runs README Quick start steps 1 to 6 once (safe to run again),
# then keeps the server and its two background jobs running.
set -euo pipefail
S=/state
mkdir -p "$S"
export BASE=http://127.0.0.1:8765

python apps/core/sqlmigrate.py --dsn "$MESHY_DSN"
python apps/core/sqlmigrate.py --dsn "$MESHY_DSN" --check

if [ ! -f "$S/jwt.pem" ]; then
  python - <<'PY'
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
open("/state/jwt.pem", "wb").write(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
open("/state/jwt.pub.pem", "wb").write(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo))
PY
  python -c "import secrets; print(secrets.token_hex(32))" > "$S/state_secret"
  python -c "import secrets; print(secrets.token_hex(32))" > "$S/secret_key"
fi

if [ ! -f "$S/bootstrapped" ]; then
  G=$(python -c "import uuid; print(uuid.uuid4())")
  python -m tools.meshy_node keygen --base-url "$BASE" --state-file "$S/operator.json" --public-key-out "$S/operator.pub" > /dev/null
  python manage.py bootstrap_owner --email ops@example.org --public-key-file "$S/operator.pub" \
    --plane dev --trust-group "$G" --tier enterprise > "$S/owner.out"
  grep '^{' "$S/owner.out" > "$S/owner.json"
  python manage.py bootstrap_memory_peer --trust-group "$G" --owner-email memory@example.org
  touch "$S/bootstrapped"
fi
PEER_ID=$(python -c "import json; print(json.load(open('/state/owner.json'))['peer_id'])")

export MESHYCHAT_JWT_ISSUER=meshychat-quickstart
export MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH=$S/jwt.pem
export MESHYCHAT_JWT_PUBLIC_KEY_PEM_PATH=$S/jwt.pub.pem
export MESHYCHAT_STATE_SECRET=$(cat "$S/state_secret")
export MESHY_SECRET_KEY=$(cat "$S/secret_key")
export MESHY_ALLOWED_HOSTS=127.0.0.1,localhost
export MESHYCHAT_BOOTSTRAP_AUTHORITY_PEER_ID=$PEER_ID
export MESHY_EMBED_FORBIDDEN_ROWS=true

daphne -b 0.0.0.0 -p 8765 meshychat.asgi:application &
python manage.py run_jobs --loop --interval 5 &
python manage.py run_call_worker &

for _ in $(seq 1 60); do
  python -c "import urllib.request; urllib.request.urlopen('$BASE/v1/livez', timeout=2)" 2>/dev/null && break
  sleep 1
done

if [ ! -f "$S/claimed" ]; then
  python -m tools.meshy_node bind  --base-url "$BASE" --state-file "$S/operator.json" --peer-id "$PEER_ID"
  python -m tools.meshy_node claim --base-url "$BASE" --state-file "$S/operator.json" --namespace shared/mesh-chat
  touch "$S/claimed"
fi
echo "MeshyChat is up at $BASE (operator peer $PEER_ID). Try: docker compose exec meshychat bash deploy/quickstart/demo.sh"
wait -n
