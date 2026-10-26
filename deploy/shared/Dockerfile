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
COPY requirements-shared.lock requirements-reports.lock ./
RUN apt-get update -qq && apt-get install -y --no-install-recommends fonts-dejavu-core=2.37-6 fonts-dejavu-extra=2.37-6 && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --require-hashes -r requirements-shared.lock -r requirements-reports.lock
COPY --from=wheel /build/dist/*.whl /wheels/
RUN python -m pip install --no-deps /wheels/*.whl
ENTRYPOINT ["python", "-m", "memoryking.shared"]
