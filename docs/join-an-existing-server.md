# Join an existing server (client only)

Use this when someone already runs a MeshyChat server and you only need a node on your own machine. You do not install PostgreSQL and you do not run the server.

**You need:** CPython 3.13, the server's URL, and a pairing code from the server's operator.

```bash
git clone https://github.com/ademczuk/MeshyChat.git
cd MeshyChat
python3.13 -m venv .venv && . .venv/bin/activate
pip install cryptography PyNaCl rfc8785 websockets -c requirements.lock.txt
```

Those four packages are all the client imports. The server's dependencies (Django, daphne, psycopg and the rest) are not needed. Commands are bash; in PowerShell use `$env:NAME = "value"`.

```bash
export BASE=https://your-server.example   # the URL the operator gives you

# 1. Redeem the pairing code. This writes your key to node.json: keep that file private.
python -m tools.meshy_node mint --base-url "$BASE" --state-file node.json --code <code> --device-label my-laptop
```

`mint` prints your `peer_id`. Send it to the operator: they have to grant you the mail namespace before you can write.

```bash
# Operator, on their side:
python -m tools.meshy_node grant --base-url "$BASE" --state-file operator.json --namespace shared/mesh-chat --grantee <your peer_id> --permission write
```

Then, on your machine:

```bash
python -m tools.meshy_node listen --base-url "$BASE" --state-file node.json --read
python -m tools.meshy_node send   --base-url "$BASE" --state-file node.json --to <their label> --thread hello --type request --expects-reply --body "Hello"
python -m tools.meshy_node owed   --base-url "$BASE" --state-file node.json
```

A node's label is its `peers.name` or `peer-` plus the first eight characters of its `peer_id`.

**How this was tested:** against the server from [quickstart-docker.md](quickstart-docker.md), from a clean `python:3.13-slim` container with only the four packages above installed, sharing the server's network. `mint`, `send` and `listen` (connecting and receiving the server's welcome frame) all succeeded. Not tested: a client on Windows or macOS, a server behind TLS or a proxy, and reading mail you receive from another node with this client-only install.
