# cogame-factorio Coworld image: game server + Factorio headless + bundled
# players + static replay viewer in one image.
#
# Stage 1 (wasm-builder) compiles the static replay viewer with
# emscripten (viewer/build_viewer.sh -> viewer/dist). Wasm is
# architecture-independent, so this stage runs on the build host's native
# platform ($BUILDPLATFORM) — no qemu for the compile on ARM hosts.
#
# Stage 2 is the linux/amd64 runtime: python:3.11-slim + locked deps via
# uv + the Factorio 1.1.110 headless server (downloaded from factorio.com
# at build time — free, no auth; never committed or redistributed) at
# /opt/factorio with FLE's lab scenario + server config installed next to
# it. The repo layout is preserved at /workspace (server code resolves
# viewer/dist relative to the repo root; PYTHONPATH covers server/ and
# players/), so the project is NOT pip-installed into site-packages.
#
# Entrypoints (Coworld manifest `run`):
#   game             python -m cogame_factorio.server
#   idle player      python -m players.idle_player
#   handcraft player python -m players.handcraft_player
#   burner player    python -m players.burner_player
#
# Build: docker build --platform=linux/amd64 -t cogame-factorio:local .

# Pin matches the emcc used for local viewer builds.
FROM --platform=$BUILDPLATFORM emscripten/emsdk:6.0.5 AS wasm-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl unzip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Prefetch the raylib web build into the exact cache location
# viewer/build_viewer.sh expects, as its own layer so source edits never
# re-download. URL/sha MUST stay in sync with viewer/build_viewer.sh; if
# they drift, build_viewer.sh just re-fetches (correct, slower).
ARG RAYLIB_ZIP_URL="https://github.com/raysan5/raylib/releases/download/5.5/raylib-5.5_webassembly.zip"
ARG RAYLIB_ZIP_SHA256="798b6bea650e78a60fe49f106a15d92ea4e33efd3aa1b3efa34b0438a14bbf2c"
RUN mkdir -p build && \
    curl -fsSL --retry 3 "$RAYLIB_ZIP_URL" -o /tmp/raylib-web.zip && \
    echo "$RAYLIB_ZIP_SHA256  /tmp/raylib-web.zip" | sha256sum -c - && \
    (cd build && unzip -q /tmp/raylib-web.zip && \
     mv raylib-5.5_webassembly raylib-web && \
     printf '%s\n' "$RAYLIB_ZIP_SHA256" > raylib-web/.zip-sha256) && \
    rm /tmp/raylib-web.zip

COPY viewer/ viewer/

# viewer/build_viewer.sh produces viewer/dist/. Until the viewer exists
# (or in a checkout without it) fall back to a placeholder page so the
# game image still builds and replay mode serves something.
RUN if [ -f viewer/build_viewer.sh ]; then \
        bash viewer/build_viewer.sh; \
    else \
        mkdir -p viewer/dist && \
        echo '<!doctype html><title>viewer not built</title>' > viewer/dist/index.html; \
    fi && test -f viewer/dist/index.html


FROM python:3.11-slim

WORKDIR /workspace

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Factorio 1.1.110 headless (linux64). Downloaded and sha256-verified at
# build time; installed read-only at /opt/factorio. Each seat's server
# gets its own writable write-data dir at runtime (see factorio.py).
ARG FACTORIO_VERSION=1.1.110
ARG FACTORIO_URL="https://factorio.com/get-download/${FACTORIO_VERSION}/headless/linux64"
ARG FACTORIO_SHA256="485fe6db36e5decd7dd0d70e7c97e61f818100fa3e48d87884b287027c7a646a"
RUN curl -fsSL --retry 3 "$FACTORIO_URL" -o /tmp/factorio_headless.tar.xz && \
    echo "$FACTORIO_SHA256  /tmp/factorio_headless.tar.xz" | sha256sum -c - && \
    tar -xJf /tmp/factorio_headless.tar.xz -C /opt && \
    rm /tmp/factorio_headless.tar.xz && \
    test -x /opt/factorio/bin/x64/factorio

# Locked runtime deps only (aiohttp + FLE 0.3.0 and its tree): the project
# itself stays at /workspace via PYTHONPATH. uv is bind-mounted from its
# distribution image for this RUN only so it never becomes a layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.9.18,source=/uv,target=/usr/local/bin/uv \
    uv sync --frozen --no-dev --no-install-project

# FLE's lab scenario (default_lab_scenario) and Factorio server config
# (server-settings.json with allow_commands=true, map settings, admin
# list) come from the installed fle package and must sit next to the
# Factorio install (scenarios are resolved through each seat's write-data
# dir symlink; config paths are passed explicitly).
RUN FLE_DIR="$(/workspace/.venv/bin/python -c 'import fle, os; print(os.path.dirname(fle.__file__))')" && \
    mkdir -p /opt/factorio/scenarios /opt/factorio/config && \
    cp -r "$FLE_DIR/cluster/scenarios/." /opt/factorio/scenarios/ && \
    cp -r "$FLE_DIR/cluster/config/." /opt/factorio/config/ && \
    test -d /opt/factorio/scenarios/default_lab_scenario && \
    test -f /opt/factorio/config/server-settings.json

ENV PATH="/workspace/.venv/bin:$PATH" \
    PYTHONPATH="/workspace/server:/workspace" \
    PYTHONUNBUFFERED=1 \
    COGAME_FACTORIO_ROOT=/opt/factorio

COPY server/ server/
COPY players/ players/
COPY --from=wasm-builder /src/viewer/dist/ viewer/dist/

CMD ["python", "-m", "cogame_factorio.server"]
