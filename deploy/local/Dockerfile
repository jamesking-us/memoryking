# MemoryKing local daemon: the release wheel on a digest-pinned Python image, core lock only.
# Build from the repository root:  docker build -f deploy/local/Dockerfile -t memoryking:local .
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS wheel
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_PROGRESS_BAR=off
WORKDIR /build
COPY requirements-build.lock pyproject.toml README.md ./
RUN python -m pip install --require-hashes -r requirements-build.lock
COPY src ./src
RUN python -m build --wheel --no-isolation

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_PROGRESS_BAR=off
WORKDIR /app
COPY requirements-core.lock ./
RUN python -m pip install --require-hashes -r requirements-core.lock
COPY --from=wheel /build/dist/*.whl /wheels/
RUN python -m pip install --no-deps /wheels/*.whl \
 && groupadd -g 1000 memoryking && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin memoryking \
 && mkdir -p /data /control && chown memoryking:memoryking /data /control && chmod 700 /data /control
USER memoryking
VOLUME ["/data", "/control"]
# The store and control trees are created inside the volumes (a volume's mount point
# already exists, and init insists on new directories); the socket is
# /data/store/service.sock. The dashboard binds loopback only, so publish it with
# --network host or reach it from inside the container. Run `init` once before `serve`.
ENTRYPOINT ["python", "-m", "memoryking"]
CMD ["serve", "--store", "/data/store", "--control", "/control/store", "--dashboard-port", "8771"]
