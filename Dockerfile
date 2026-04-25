FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ARG RUNNER_VERSION=2.333.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git jq rsync zip unzip sudo lsb-release gnupg \
    libicu-dev libssl-dev zlib1g-dev build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Python 3.12 (default in 24.04)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Azure CLI
RUN curl -sL https://aka.ms/InstallAzureCLIDeb | bash

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (daemon runs on host, socket is mounted)
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update && apt-get install -y docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

# Runner user
RUN groupadd -f docker \
    && useradd -m -s /bin/bash runner \
    && usermod -aG sudo,docker runner \
    && echo "runner ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# GitHub Actions runner
RUN cd /home/runner \
    && curl -fsSL "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz" \
    | tar xz \
    && chown -R runner:runner /home/runner

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER runner
WORKDIR /home/runner

ENTRYPOINT ["/entrypoint.sh"]
