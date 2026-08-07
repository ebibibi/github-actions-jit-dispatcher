FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ARG RUNNER_VERSION=2.335.1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git jq rsync zip unzip sudo lsb-release gnupg \
    libicu-dev libssl-dev zlib1g-dev build-essential libffi-dev \
    # Playwright/Chromium system dependencies
    libglib2.0-0t64 libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 \
    libcups2t64 libdrm2 libdbus-1-3 libexpat1 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
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

# Bicep CLI
# New-AzDeployment -TemplateFile *.bicep は Bicep CLI を要求する。
# 無いと「Cannot retrieve the dynamic parameters ... Please add Bicep」で失敗する。
# GitHub ホストランナーには標準搭載されているため、self-hosted へ移行した
# 時点で気づかないまま壊れる（2026-08-07 に graph-api-proxy / m365reporting で発生）。
# az bicep install は実行ユーザーの ~/.azure/bin に入るため、root でビルドすると
# runner ユーザーから見えない。全ユーザーが使えるよう /usr/local/bin に直接置く。
RUN curl -sLo /usr/local/bin/bicep \
      "https://github.com/Azure/bicep/releases/latest/download/bicep-linux-$(dpkg --print-architecture | sed 's/amd64/x64/')" \
    && chmod +x /usr/local/bin/bicep \
    && bicep --version

# az CLI は既定で ~/.azure/bin の bicep を探すため、PATH 上の bicep を使わせる。
# これが無いと az bicep 系のコマンドだけ「Bicep CLI not found」になる。
ENV AZURE_BICEP_USE_BINARY_FROM_PATH=true

# PowerShell Core (required by azure/powershell@v2 for Bicep deployments)
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget apt-transport-https software-properties-common \
    && wget -q "https://packages.microsoft.com/config/ubuntu/$(lsb_release -rs)/packages-microsoft-prod.deb" \
    && dpkg -i packages-microsoft-prod.deb \
    && rm packages-microsoft-prod.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends powershell \
    && rm -rf /var/lib/apt/lists/*

# Az PowerShell モジュール
# azure/login の enable-AzPSSession: true は Az.Accounts を要求する。
# PowerShell Core を入れただけでは足りず、モジュールが無いと
# Connect-AzAccount が "Cannot bind argument to parameter 'Name' because it is null"
# で必ず失敗する（2026-08-07 に graph-api-proxy / m365reporting で発生）。
# GitHub ホストランナーには最初から入っているため、self-hosted へ移行した
# 時点で気づかないまま壊れる。
# azure/powershell@v2 は azPSVersion の解決に **Az アンブレラモジュール**の存在を
# 前提にしている。Az.Accounts / Az.Resources だけでは足りず、Az が無いと
# アクション自身のラッパーがバージョン解決に失敗し、ユーザースクリプトを
# 実行する前に "You cannot call a method on a null-valued expression." で落ちる
# （2026-08-07 に graph-api-proxy / m365reporting で発生）。
RUN pwsh -NoProfile -Command \
      "Set-PSRepository -Name PSGallery -InstallationPolicy Trusted; \
       Install-Module -Name Az -RequiredVersion 16.2.0 -Scope AllUsers -Force"

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
