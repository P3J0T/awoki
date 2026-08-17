FROM golang:1.26.5-bookworm AS go_semantics_builder
WORKDIR /src
COPY .harness/code_search/go_semantics_probe.go /src/main.go
RUN CGO_ENABLED=0 GO111MODULE=off GOTOOLCHAIN=local go build -trimpath -ldflags="-s -w" -o /out/awoki-go-semantics /src/main.go \
    && /out/awoki-go-semantics --version | grep -E '^awoki-go-semantics go1\.26\.5$'

FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AWOKI_EMBEDDING_PROVIDER=openai \
    AWOKI_RERANK_ENABLED=0

WORKDIR /awoki

COPY --from=go_semantics_builder /out/awoki-go-semantics /usr/local/bin/awoki-go-semantics

# Awoki is Docker-first. The image includes practical local-dev, security,
# reverse-engineering, evidence-collection, and troubleshooting utilities.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      wget \
      git \
      openssh-client \
      gnupg \
      make \
      cmake \
      ninja-build \
      build-essential \
      pkg-config \
      python3-venv \
      file \
      binutils \
      elfutils \
      bsdextrautils \
      xxd \
      coreutils \
      findutils \
      grep \
      sed \
      gawk \
      ripgrep \
      fd-find \
      jq \
      yq \
      sqlite3 \
      tree \
      less \
      vim-tiny \
      nano \
      unzip \
      zip \
      p7zip-full \
      tar \
      gzip \
      bzip2 \
      xz-utils \
      zstd \
      strace \
      gdb \
      gdbserver \
      lsof \
      procps \
      psmisc \
      iproute2 \
      iputils-ping \
      dnsutils \
      netcat-openbsd \
      nmap \
      tcpdump \
      yara \
      libimage-exiftool-perl \
      python3-magic \
    && for pkg in ltrace; do \
         if apt-cache show "$pkg" >/dev/null 2>&1; then \
           apt-get install -y --no-install-recommends "$pkg"; \
         else \
           echo "[awoki] optional apt package not available on this architecture: $pkg"; \
         fi; \
       done \
    && if command -v fdfind >/dev/null 2>&1; then ln -sf "$(command -v fdfind)" /usr/local/bin/fd; fi \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/requirements.txt

COPY . /awoki
RUN chmod +x /awoki/.harness/bin/code-parser-check /awoki/.harness/bin/code-search-eval-check \
    && AWOKI_ROOT=/awoki HARNESS_ROOT=/awoki /awoki/.harness/bin/code-parser-check \
    && AWOKI_ROOT=/awoki HARNESS_ROOT=/awoki /awoki/.harness/bin/code-search-eval-check \
    && /usr/local/bin/awoki-go-semantics --version | grep -E '^awoki-go-semantics go1\.26\.5$' \
    && AWOKI_ROOT=/awoki HARNESS_ROOT=/awoki /awoki/.harness/bin/mcp-preflight --quiet

CMD ["python", ".harness/server.py"]
