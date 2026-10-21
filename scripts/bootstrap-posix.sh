#!/usr/bin/env bash
# Sourced by the two entrypoints. No persistent shell profile or apt mirror edits.
rc_die() { echo "RemoteCode: $*" >&2; exit 1; }
rc_as_root() {
    if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -- "$@"; fi
}
rc_python() {
    local candidate
    for candidate in "${REMOTECODE_PYTHON:-}" python3.13 python3.12 python3 /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12; do
        [ -n "$candidate" ] || continue
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,12))' >/dev/null 2>&1; then
            RC_PYTHON=$(command -v "$candidate"); return 0
        fi
    done
    return 1
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
rc_docker() {
    if command -v docker >/dev/null 2>&1; then
        docker compose version >/dev/null 2>&1 || rc_die 'Existing Docker has no Compose plugin. Install its matching plugin; existing Docker packages will not be replaced.'
    else
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
    fi
    if command -v systemctl >/dev/null 2>&1; then rc_as_root systemctl enable --now docker; fi
    docker info >/dev/null || rc_die 'Docker daemon is unavailable. Cloud entrypoint must run with Docker access.'
}
rc_bootstrap() {
    local role="$1" action arg skip=0 help=0 system brewbin accepted=0 agents=1
    shift
    action=${1:-}
    RC_CN=0
    for arg in "$@"; do
        case "$arg" in
            --cn) RC_CN=1 ;;
            --skip-deps|--offline) skip=1 ;;
            -h|--help) help=1 ;;
            --accept-license) accepted=1 ;;
            --no-agents) agents=0 ;;
        esac
    done
    system=$(uname -s)
    if [ "$help" -eq 1 ]; then
        if rc_python; then return; fi
        echo "Usage: bash deploy-$role.sh {install|status|uninstall} [options]"
        echo 'Install: --cn --mirror HTTPS/{url} --cache DIR --skip-deps --offline'
        echo 'Cloud: --ip IPv4 --machine pc1 --state DIR --image IMAGE'
        echo 'Local: --bundle DIR --workspace DIR --accept-license [--no-agents] [--no-service]'
        echo 'Uninstall: [--purge] (shared dependencies and caches are retained)'
        exit 0
    fi
    case "$action" in install|status|uninstall) ;; *) rc_die 'First argument must be install, status or uninstall. Use --help.' ;; esac
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
        rc_python || rc_die 'Python 3.12+ required. Set REMOTECODE_PYTHON or install Python first.'
        return
    fi
    case "$system" in
        Linux)
            [ -r /etc/os-release ] || rc_die 'Missing /etc/os-release; preinstall dependencies and use --skip-deps.'
            . /etc/os-release
            case "$ID" in ubuntu|debian) ;; *) rc_die 'Auto dependencies support Ubuntu/Debian. Other distributions: preinstall Python 3.12+, curl, Git/Docker and use --skip-deps.' ;; esac
            # Validate Python availability before modifying the system.
            if ! rc_python; then
                case "$ID:$VERSION_ID" in
                    ubuntu:2[4-9].*|debian:1[3-9]*) ;;
                    *) rc_die 'This OS does not ship Python 3.12+. Use Ubuntu 24.04+/Debian 13+, or preinstall Python 3.12+ and rerun.' ;;
                esac
            fi
            # HTTPS apt mirrors need the system CA bundle before switching sources.
            if [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
                rc_as_root apt-get -o Acquire::Retries=3 update
                rc_as_root apt-get -o Acquire::Retries=3 install -y ca-certificates
            fi
            local packages=()
            if ! rc_python; then packages+=(python3); fi
            command -v curl >/dev/null 2>&1 || packages+=(curl)
            if [ "$role" = cloud ]; then
                command -v openssl >/dev/null 2>&1 || packages+=(openssl)
            elif [ "$agents" -eq 1 ]; then
                command -v git >/dev/null 2>&1 || packages+=(git)
            fi
            if [ "${#packages[@]}" -gt 0 ]; then
                rc_apt update
                rc_apt install -y "${packages[@]}"
            fi
            rc_python || rc_die 'Distribution Python is below 3.12; install a supported Python and rerun.'
            [ "$role" != cloud ] || rc_docker
            ;;
        Darwin)
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
