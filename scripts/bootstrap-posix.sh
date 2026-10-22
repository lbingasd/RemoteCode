#!/usr/bin/env bash
# Sourced by the two entrypoints. No persistent shell profile or apt mirror edits.
rc_die() { echo "RemoteCode: $*" >&2; exit 1; }
rc_log() { printf '[RemoteCode %s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
rc_as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -- "$@"; fi
}
rc_python() {
    local candidate
    for candidate in "${REMOTECODE_PYTHON:-}" "$RC_PACKAGE/.bootstrap/python/bin/python3" python3.13 python3.12 python3 /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,12))' >/dev/null 2>&1; then
            RC_PYTHON=$(command -v "$candidate"); return 0
        fi
    done
    return 1
}
# The private-Python bootstrap itself only needs an interpreter >=3.8 to download
# and unpack the version-locked standalone CPython 3.12.
rc_python38() {
    local candidate
    for candidate in "${REMOTECODE_PYTHON38:-}" python3 python3.12 python3.11 python3.10 python3.9 python3.8; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,8))' >/dev/null 2>&1; then
            RC_PYTHON38=$(command -v "$candidate"); return 0
        fi
    done
    return 1
}
rc_detect_os() {
    # Family and package-manager detection for the Linux branch. REMOTECODE_OS_RELEASE
    # lets tests inject a fixture; production reads the host's /etc/os-release.
    if [ -z "${ID:-}" ]; then
        if [ -r "${REMOTECODE_OS_RELEASE:-/etc/os-release}" ]; then
            . "${REMOTECODE_OS_RELEASE:-/etc/os-release}"
        fi
    fi
    RC_OS_NAME="${PRETTY_NAME:-${ID:-unknown}}"
    RC_OS_ID=$(printf '%s' "${ID:-unknown}" | tr '[:upper:]' '[:lower:]')
    RC_OS_LIKE=$(printf '%s' "${ID_LIKE:-}" | tr '[:upper:]' '[:lower:]')
    RC_OS_VERSION_ID="${VERSION_ID:-}"
    RC_OS_CODENAME="${VERSION_CODENAME:-}"
    RC_FAMILY=other
    case "$RC_OS_ID" in
        ubuntu|debian) RC_FAMILY=debian ;;
        rhel|centos|rocky|almalinux|anolis|alinux|amzn|openeuler) RC_FAMILY=rhel ;;
        opensuse*|sles|sled) RC_FAMILY=suse ;;
    esac
    if [ "$RC_FAMILY" = other ]; then
        case " $RC_OS_LIKE " in
            *" rhel "*|*" centos "*|*" fedora "*) RC_FAMILY=rhel ;;
            *" debian "*|*" ubuntu "*) RC_FAMILY=debian ;;
            *" suse "*) RC_FAMILY=suse ;;
        esac
    fi
    case "$RC_FAMILY" in
        debian) RC_PKG=apt-get ;;
        suse) RC_PKG=zypper ;;
        rhel)
            if command -v dnf >/dev/null 2>&1; then RC_PKG=dnf
            elif command -v yum >/dev/null 2>&1; then RC_PKG=yum
            else RC_PKG=dnf
            fi ;;
        *) RC_PKG='' ;;
    esac
}
rc_ensure_linux_python() {
    rc_log '[依赖 2/3] 检查 Python 3.12+'
    if rc_python; then rc_log "Python 已就绪: $RC_PYTHON"; return; fi
    rc_log '系统 Python 较旧，正在引导私有 Python；首次连接和下载可能需要等待'
    # Use an older system interpreter only to fetch a private, version-locked one.
    if ! rc_python38; then
        if [ "$RC_FAMILY" = rhel ] && [ "$RC_PKG" = dnf ]; then
            rc_log '没有 Python 3.8+，尝试安装发行版提供的新版 Python 后继续'
            # RHEL 8 family: python38; RHEL 9 / Alibaba Cloud Linux 3: python3.11.
            rc_pkg_install python38 || rc_pkg_install python3.11 || true
            rc_python38 || true
        fi
    fi
    if ! rc_python38; then
        rc_die 'Private bootstrap needs system Python 3.8+ (Ubuntu 20.04+/Debian 11+, or python3.8+ on RHEL-family).'
    fi
    local python_args=(--prefix "$RC_PACKAGE/.bootstrap/python" --cache "$RC_DOWNLOAD_CACHE/bootstrap")
    [ -z "$RC_DOWNLOAD_MIRROR" ] || python_args+=(--mirror "$RC_DOWNLOAD_MIRROR")
    PYTHONUNBUFFERED=1 "$RC_PYTHON38" "$RC_PACKAGE/scripts/bootstrap-python.py" "${python_args[@]}"
    rc_python || rc_die 'Private Python bootstrap failed; cached downloads are retained.'
}
rc_apt() {
    # Use a temporary, distro-signed source list when --cn was explicitly chosen.
    # Existing /etc/apt sources are untouched. apt keeps verified partial downloads.
    if [ "$RC_CN" -eq 1 ]; then
        local arch mirror components list
        arch=$(dpkg --print-architecture)
        components=main
        if [ "$ID" = ubuntu ]; then
            components='main restricted universe multiverse'
            mirror=https://mirrors.tuna.tsinghua.edu.cn/ubuntu
            [ "$arch" != arm64 ] || mirror=https://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports
        else
            mirror=https://mirrors.tuna.tsinghua.edu.cn/debian
        fi
        list=$(mktemp /tmp/remotecode-apt.XXXXXX)
        printf 'deb %s %s %s\n' "$mirror" "$VERSION_CODENAME" "$components" > "$list"
        if [ "$ID" = ubuntu ]; then
            printf 'deb %s %s-updates %s\ndeb %s %s-security %s\n' "$mirror" "$VERSION_CODENAME" "$components" "$mirror" "$VERSION_CODENAME" "$components" >> "$list"
        else
            printf 'deb %s %s-updates main\ndeb https://mirrors.tuna.tsinghua.edu.cn/debian-security %s-security main\n' "$mirror" "$VERSION_CODENAME" "$VERSION_CODENAME" >> "$list"
        fi
        chmod 644 "$list"
        rc_as_root apt-get -o "Dir::Etc::sourcelist=$list" -o 'Dir::Etc::sourceparts=-' -o Acquire::Retries=3 "$@" || { rm -f "$list"; return 1; }
        rm -f "$list"
    else
        rc_as_root apt-get -o Acquire::Retries=3 "$@"
    fi
}
# Thin package-manager dispatch. Only apt needs an explicit refresh before install
# (its --cn path adds a fresh source list); dnf/yum refresh metadata on install.
rc_pkg_update() {
    [ "$RC_PKG" != apt-get ] || rc_apt update
}
rc_pkg_install() {
    case "$RC_PKG" in
        apt-get) rc_apt install -y "$@" ;;
        dnf) rc_as_root dnf install -y "$@" ;;
        yum) rc_as_root yum install -y "$@" ;;
        zypper) rc_as_root zypper --non-interactive install "$@" ;;
        *) rc_die '未知包管理器；预装依赖后使用 --skip-deps' ;;
    esac
}
rc_docker_compose() {
    # Reuses existing Docker; fills a missing Compose plugin with the standalone binary.
    if docker compose version >/dev/null 2>&1; then
        rc_log '已有 Docker Compose 可用，直接复用'
        return
    fi
    rc_log '已有 Docker 缺少可用 Compose，安装独立插件，保留现有 Docker 软件包'
    local compose_args=(--cache "$RC_DOWNLOAD_CACHE/bootstrap")
    [ -z "$RC_DOWNLOAD_MIRROR" ] || compose_args+=(--mirror "$RC_DOWNLOAD_MIRROR")
    rc_as_root "$RC_PYTHON" -u "$RC_PACKAGE/scripts/bootstrap-compose.py" "${compose_args[@]}"
    docker compose version || rc_die 'Compose plugin is installed but not discoverable. Check Docker CLI version and higher-priority plugins in DOCKER_CONFIG/cli-plugins.'
}
rc_docker_debian() {
    rc_log '准备 Docker 软件源与安装包；下方显示 apt/curl 原始进度'
    # Do not remove a competing container runtime on the user's behalf.
    local pkg installed repo key list
    for pkg in docker.io podman-docker containerd runc; do
        installed=$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)
        [ "$installed" != 'install ok installed' ] || rc_die "Conflicting package $pkg exists; configure Docker manually or use --skip-deps."
    done
    # Isolated repository files; no daemon.json changes and no daemon restart.
    repo="https://download.docker.com/linux/$ID"
    [ "$RC_CN" -eq 0 ] || repo="https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/$ID"
    key=/etc/apt/keyrings/remotecode-docker.asc
    list=/etc/apt/sources.list.d/remotecode-docker.list
    # Reuse existing upstream repository setup rather than introduce duplicates.
    if ! grep -RqsE '^[^#]*(download\.docker\.com|docker-ce/linux)' /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
        rc_as_root install -d -m 0755 /etc/apt/keyrings
        local keytemp
        keytemp=$(mktemp /tmp/remotecode-docker-key.XXXXXX)
        curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 15 --max-time 120 "$repo/gpg" -o "$keytemp" || { rm -f "$keytemp"; return 1; }
        rc_as_root install -m 0644 "$keytemp" "$key"
        rm -f "$keytemp"
        printf 'deb [arch=%s signed-by=%s] %s %s stable\n' "$(dpkg --print-architecture)" "$key" "$repo" "$VERSION_CODENAME" | rc_as_root tee "$list" >/dev/null
    fi
    # Include the Docker repository and leave every preexisting source intact.
    rc_as_root apt-get -o Acquire::Retries=3 update
    rc_as_root apt-get -o Acquire::Retries=3 install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}
rc_docker_rhel_ce() {
    # centos-compatible IDs whose $releasever matches the Docker CE centos repo.
    rc_log '准备 Docker CE 软件源与安装包；下方显示 dnf/curl 原始进度'
    local repo key list
    repo="https://download.docker.com/linux/centos"
    [ "$RC_CN" -eq 0 ] || repo="https://mirrors.tuna.tsinghua.edu.cn/docker-ce/linux/centos"
    key=/etc/pki/rpm-gpg/remotecode-docker
    list=/etc/yum.repos.d/remotecode-docker.repo
    # Reuse existing upstream repository setup rather than introduce duplicates.
    if ! grep -RqsE '^[^#]*(download\.docker\.com|docker-ce/linux)' /etc/yum.repos.d 2>/dev/null; then
        rc_as_root install -d -m 0755 /etc/pki/rpm-gpg
        local keytemp
        keytemp=$(mktemp /tmp/remotecode-docker-key.XXXXXX)
        curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 15 --max-time 120 "$repo/gpg" -o "$keytemp" || { rm -f "$keytemp"; return 1; }
        rc_as_root install -m 0644 "$keytemp" "$key"
        rm -f "$keytemp"
        printf '[remotecode-docker]\nname=Docker CE Stable - %s\nbaseurl=%s/$releasever/$basearch/stable\ngpgcheck=1\ngpgkey=file://%s\nenabled=1\n' "$RC_OS_ID" "$repo" "$key" | rc_as_root tee "$list" >/dev/null
    fi
    rc_pkg_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}
rc_docker_rhel() {
    rc_log '准备 Docker 软件源与安装包；下方显示 dnf/rpm 原始进度'
    # Do not remove a competing container runtime on the user's behalf.
    local pkg
    for pkg in docker docker-ce docker-engine podman-docker; do
        rpm -q "$pkg" >/dev/null 2>&1 || continue
        rc_die "Conflicting package $pkg exists; configure Docker manually or use --skip-deps."
    done
    case "$RC_OS_ID" in
        rhel|centos|rocky|almalinux)
            rc_docker_rhel_ce
            ;;
        amzn|openeuler|anolis|alinux)
            # Vendor repositories already carry a `docker` package (openEuler ships it as
            # a metapackage over docker-engine + docker-client; Amazon Linux 2023 ships its
            # own build). Compose is filled in later by rc_docker_compose where absent.
            if ! rc_pkg_install docker; then
                rc_die "发行版仓库没有可用的 docker 包（${RC_OS_NAME}）；预装 Docker 后使用 --skip-deps"
            fi
            ;;
        *) rc_die "自动安装 Docker 暂不支持 ${RC_OS_NAME}；预装 Docker 后使用 --skip-deps" ;;
    esac
}
rc_docker_install() {
    case "$RC_FAMILY" in
        debian) rc_docker_debian ;;
        rhel) rc_docker_rhel ;;
        *) rc_die '自动安装 Docker 仅支持 Ubuntu/Debian 与 RHEL 家族；预装 Docker 后使用 --skip-deps' ;;
    esac
}
rc_docker() {
    rc_log '[依赖 3/3] 检查 Docker Engine 和 Compose'
    if command -v docker >/dev/null 2>&1; then
        rc_docker_compose
    else
        rc_docker_install
        rc_docker_compose
    fi
    rc_log '启动 Docker 并检查引擎连接'
    if command -v systemctl >/dev/null 2>&1; then rc_as_root systemctl enable --now docker; fi
    docker info >/dev/null || rc_die 'Docker daemon is unavailable. Cloud entrypoint must run with Docker access.'
}
rc_bootstrap() {
    local role="$1" action arg skip=0 help=0 system brewbin accepted=0 agents=1 previous='' address=''
    shift
    action=${1:-}
    RC_CN=0
    RC_DOWNLOAD_MIRROR=''
    RC_DOWNLOAD_CACHE="$RC_PACKAGE/downloads"
    for arg in "$@"; do
        if [ "$previous" = --mirror ]; then RC_DOWNLOAD_MIRROR="$arg"; previous=''; continue; fi
        if [ "$previous" = --cache ]; then RC_DOWNLOAD_CACHE="$arg"; previous=''; continue; fi
        if [ "$previous" = --ip ]; then address="$arg"; previous=''; continue; fi
        case "$arg" in
            --cn) RC_CN=1 ;;
            --skip-deps|--offline) skip=1 ;;
            -h|--help) help=1 ;;
            --accept-license) accepted=1 ;;
            --no-agents) agents=0 ;;
            --mirror|--cache|--ip) previous="$arg" ;;
            --mirror=*) RC_DOWNLOAD_MIRROR=${arg#--mirror=} ;;
            --cache=*) RC_DOWNLOAD_CACHE=${arg#--cache=} ;;
            --ip=*) address=${arg#--ip=} ;;
        esac
    done
    # Also covers Python/Compose downloads before setup.py starts.
    export REMOTECODE_CN="$RC_CN"
    [ -z "$previous" ] || rc_die "$previous requires a value."
    system=$(uname -s)
    if [ "$help" -eq 1 ]; then
        if rc_python; then return; fi
        echo "Usage: bash deploy-$role.sh {install|status|uninstall} [options]"
        echo 'Install: --cn --mirror HTTPS/{url} --cache DIR --skip-deps --offline'
        echo 'Cloud: --ip IPv4 --machine pc1 --state DIR --image IMAGE'
        echo 'Local: --bundle DIR --workspace DIR --accept-license [--no-agents] [--no-service] [--qq-bot]'
        echo 'Uninstall: [--purge] (shared dependencies and caches are retained)'
        exit 0
    fi
    case "$action" in install|status|uninstall) ;; *) rc_die 'First argument must be install, status or uninstall. Use --help.' ;; esac
    if [ "$role" = cloud ] && [ "$action" = install ] && [ -z "$address" ]; then
        rc_die 'Cloud install requires --ip IPv4. Do not paste the shell > prompt into the command.'
    fi
    rc_log "开始 ${role} / ${action}；正在检查系统和依赖"
    if [ "$role" = local ] && [ "$action" = install ] && [ "$accepted" -eq 0 ]; then
        rc_die 'Read the Microsoft VS Code Server license and explicitly pass --accept-license.'
    fi
    if [ "$role" = cloud ]; then
        [ "$system" = Linux ] || rc_die 'The automatic gateway installer requires Linux. On Windows Server/macOS, run it inside a Linux VM.'
        if [ "$(id -u)" -ne 0 ] && [ "$skip" -eq 0 ]; then
            exec sudo -H -- bash "$RC_PACKAGE/deploy-cloud.sh" "$@"
        fi
    else
        [ "$(id -u)" -ne 0 ] || rc_die 'Run the home installer as your normal developer user, without sudo.'
    fi
    # Uninstall/status/help never install packages, fetch assets, or change apt sources.
    if [ "$action" != install ] || [ "$skip" -eq 1 ]; then
        rc_log '跳过系统依赖安装，查找已有 Python'
        rc_python || rc_die 'Python 3.12+ required. Set REMOTECODE_PYTHON or install Python first.'
        return
    fi
    case "$system" in
        Linux)
            rc_log '[依赖 1/3] 检查 Linux 系统、CA 和基础工具'
            [ -r "${REMOTECODE_OS_RELEASE:-/etc/os-release}" ] || rc_die 'Missing /etc/os-release; preinstall dependencies and use --skip-deps.'
            rc_detect_os
            case "$RC_FAMILY" in
                debian|rhel) ;;
                *) rc_die "Auto dependencies support Ubuntu/Debian and RHEL-family (dnf) server systems; detected ${RC_OS_NAME}. Other distributions: preinstall Python 3.12+, curl, Git/Docker and use --skip-deps." ;;
            esac
            # HTTPS package/download mirrors need the system CA bundle before switching sources.
            if [ ! -s /etc/ssl/certs/ca-certificates.crt ] && [ ! -s /etc/pki/tls/certs/ca-bundle.crt ]; then
                rc_pkg_update
                rc_pkg_install ca-certificates
            fi
            local packages=()
            command -v python3 >/dev/null 2>&1 || packages+=(python3)
            command -v curl >/dev/null 2>&1 || packages+=(curl)
            if [ "$role" = cloud ]; then
                command -v openssl >/dev/null 2>&1 || packages+=(openssl)
            elif [ "$agents" -eq 1 ]; then
                command -v git >/dev/null 2>&1 || packages+=(git)
            fi
            if [ "${#packages[@]}" -gt 0 ]; then
                rc_log "需要安装基础工具: ${packages[*]}；下方显示 $RC_PKG 原始进度"
                rc_pkg_update
                rc_pkg_install "${packages[@]}"
            fi
            rc_ensure_linux_python
            [ "$role" != cloud ] || rc_docker
            ;;
        Darwin)
            rc_log '检查 macOS 的 Python、Node.js 和 Git'
            # A complete existing toolchain does not require installing Homebrew.
            if rc_python; then
                if [ "$agents" -eq 0 ]; then return; fi
                if command -v node >/dev/null 2>&1 && node -e 'process.exit(parseInt(process.versions.node) < 22 ? 1 : 0)' >/dev/null 2>&1 && command -v git >/dev/null 2>&1; then
                    return
                fi
            fi
            if [ "$RC_CN" -eq 1 ]; then
                export HOMEBREW_BREW_GIT_REMOTE=https://mirrors.tuna.tsinghua.edu.cn/git/homebrew/brew.git
                export HOMEBREW_API_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api
                export HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles
                export HOMEBREW_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
            fi
            brewbin=$(command -v brew || true)
            [ -n "$brewbin" ] || { [ ! -x /opt/homebrew/bin/brew ] || brewbin=/opt/homebrew/bin/brew; }
            [ -n "$brewbin" ] || { [ ! -x /usr/local/bin/brew ] || brewbin=/usr/local/bin/brew; }
            if [ -z "$brewbin" ]; then
                # The official installer may require Apple's CLT UI and sudo.
                if ! xcode-select -p >/dev/null 2>&1; then
                    xcode-select --install || true
                    rc_die 'Finish Apple Command Line Tools installation, then rerun this same command.'
                fi
                local installer
                installer=$(mktemp /tmp/remotecode-brew.XXXXXX)
                curl --fail --location --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 15 --max-time 120 https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh -o "$installer" || { rm -f "$installer"; return 1; }
                NONINTERACTIVE=1 /bin/bash "$installer" || { rm -f "$installer"; return 1; }
                rm -f "$installer"
                brewbin=/opt/homebrew/bin/brew
                [ -x "$brewbin" ] || brewbin=/usr/local/bin/brew
            fi
            export PATH="$($brewbin --prefix)/bin:$PATH"
            if ! rc_python; then "$brewbin" install python@3.12; fi
            if [ "$agents" -eq 1 ]; then
                if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(parseInt(process.versions.node) < 22 ? 1 : 0)' >/dev/null 2>&1; then
                    "$brewbin" install node@22
                    export PATH="$($brewbin --prefix node@22)/bin:$PATH"
                fi
            fi
            command -v git >/dev/null 2>&1 || "$brewbin" install git
            rc_python || rc_die 'Python installation failed.'
            ;;
        *) rc_die 'Use deploy-local.ps1 on Windows.' ;;
    esac
}
