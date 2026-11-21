# Quick start with Docker

One command starts a throwaway MeshyChat server with its own PostgreSQL 16. A second command runs a demo: two nodes, one ask that needs an answer, answered and closed.

You need Docker with Compose. Nothing else is installed on your machine.

```bash
git clone https://github.com/ademczuk/MeshyChat.git
cd MeshyChat
docker compose -f deploy/quickstart/compose.yaml up -d --build
docker compose -f deploy/quickstart/compose.yaml exec meshychat bash deploy/quickstart/demo.sh
```

The first command builds the image and starts both containers in the background. Watch it with `docker compose -f deploy/quickstart/compose.yaml logs -f meshychat` until it prints `MeshyChat is up`. On the machine this was tested on, that took 33 seconds with the base image cached, and the demo took about another minute and a half.

**What the demo does**

- Pairs and mints two nodes (`node-a`, `node-b`) and grants both the `shared/mesh-chat` namespace.
- `node-b` listens; `node-a` sends it a request that expects a reply. The listener prints the signed message.
- `node-b`'s `owed` list shows the ask as open. `node-b` answers with `--closes`, and the list goes back to `open 0`.

The owed list is filled in by a background job, so those two lines can take a few seconds.

**What it sets up for you**

These are the steps of the README Quick start, run by `deploy/quickstart/entrypoint.sh`: schema migration into an empty database, a fresh token key pair, the operator key, the first owner and the memory authority, the server (daphne), the two background jobs, and the claim of `shared/mesh-chat`. The state (keys and the node files) lives in a Docker volume called `quickstart_state`.

**Stop and wipe everything**

```bash
docker compose -f deploy/quickstart/compose.yaml down -v
```

**Read this before reusing it**

This is for trying MeshyChat on one machine. The database password is a fixed throwaway value, the server only listens on `127.0.0.1`, and semantic search is off because no model endpoints are configured (see the end of the README Quick start). For a real install, follow the README Quick start.

To connect a client from another machine to a server that already runs, see [join-an-existing-server.md](join-an-existing-server.md).
