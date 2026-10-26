FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS wheel
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_PROGRESS_BAR=off
WORKDIR /build
COPY requirements-build.lock pyproject.toml README.md ./
RUN python -m pip install --require-hashes -r requirements-build.lock
COPY src ./src
RUN python -m build --wheel --no-isolation

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_PROGRESS_BAR=off
WORKDIR /app
COPY requirements-shared.lock requirements-reports.lock ./
RUN apt-get update -qq && apt-get install -y --no-install-recommends fonts-dejavu-core=2.37-6 fonts-dejavu-extra=2.37-6 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --require-hashes -r requirements-shared.lock -r requirements-reports.lock
COPY --from=wheel /build/dist/*.whl /wheels/
RUN python -m pip install --no-deps /wheels/*.whl && \
    groupadd --gid 10001 memoryking && useradd --uid 10001 --gid 10001 --no-create-home memoryking && \
    install -d -o 10001 -g 10001 -m 0700 /data /control /artifacts /secrets /pgadmin /archives
USER 10001:10001
ENTRYPOINT ["python", "-m", "memoryking.home"]

FROM tailscale/tailscale:v1.104.2@sha256:b11583ad2e93e4671128538c88cf11f460444a5d7e449311dd1933a400ab849d AS gateway
RUN mkdir -p /var/lib/tailscale && chown 10002:10002 /var/lib/tailscale && chmod 0700 /var/lib/tailscale
USER 10002:10002
ENTRYPOINT ["/usr/local/bin/tailscaled"]

FROM postgres:18.6-bookworm@sha256:bbf83594798e144bff365a9c191c5d779f181fb3135902064ab8ff11918020fc AS database
RUN chown 10001:10001 /var/lib/postgresql && chmod 0700 /var/lib/postgresql
USER 10001:10001
