#!/bin/bash

set -euo pipefail

REPO_URL="https://github.com/malandr/telecli.git"
REF="main"
PREFIX="${HOME}/.local/share/telecli"
BIN_DIR="${HOME}/.local/bin"
STATE_DIR="${HOME}/.local/state/telecli"
SKIP_SYSTEM_PACKAGES="false"
LAUNCHER_NAME="telecli"
INSTALL_LABEL="Linux"
START_AT_STARTUP="false"

usage() {
    cat <<'EOF'
Usage: scripts/install-linux.sh [options]

Install TeleCLI on a Linux host and create a telecli launcher.

Options:
  --repo-url URL            Git repository to clone or update
  --ref REF                 Git branch or tag to install
  --prefix PATH             Install directory
  --bin-dir PATH            Directory for the telecli launcher
  --state-dir PATH          Runtime state directory for pid/log files
  --skip-system-packages    Skip apt-based dependency installation
  --help                    Show this help text

Environment:
  TELECLI_DRY_RUN=1                         Print the install plan without changing the machine
  TELECLI_AUTO_CONFIG=1                     Skip prompts and use TELECLI_INSTALL_* values/defaults
  TELECLI_INSTALL_TELEGRAM_BOT_TOKEN=...    Seed TELEGRAM_BOT_TOKEN
  TELECLI_INSTALL_ALLOWED_TELEGRAM_USERS=... Seed ALLOWED_TELEGRAM_USERS
  TELECLI_INSTALL_WEB_HOST=...              Seed WEB_HOST
  TELECLI_INSTALL_WEB_PORT=...              Seed WEB_PORT
  TELECLI_INSTALL_AUTH_REQUIRED=true|false  Seed AUTH_REQUIRED
  TELECLI_INSTALL_AUTH_TOKEN=...            Seed AUTH_TOKEN
  TELECLI_INSTALL_AI_PROXY_ENABLED=true|false Seed AI_PROXY_ENABLED
  TELECLI_INSTALL_AI_PROXY_PROVIDER=...     Seed AI_PROXY_PROVIDER
  TELECLI_INSTALL_START_AT_STARTUP=true|false Enable a user systemd startup service
EOF
}

is_dry_run() {
    [ "${TELECLI_DRY_RUN:-0}" = "1" ]
}

auto_config() {
    [ "${TELECLI_AUTO_CONFIG:-0}" = "1" ]
}

log() {
    printf '%s\n' "$*"
}

run_cmd() {
    local rendered
    printf -v rendered '%q ' "$@"

    if is_dry_run; then
        log "[dry-run] ${rendered% }"
        return 0
    fi

    "$@"
}

write_text() {
    local destination="$1"
    local content="$2"

    if is_dry_run; then
        log "[dry-run] write ${destination}"
        return 0
    fi

    printf '%s' "${content}" > "${destination}"
}

can_prompt() {
    [ -r /dev/tty ]
}

placeholder_to_blank() {
    case "$1" in
        your_telegram_bot_token_here|your_auth_token_here)
            printf '%s' ""
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

normalize_bool() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|y|on)
            printf 'true'
            ;;
        0|false|no|n|off)
            printf 'false'
            ;;
        *)
            printf '%s' "${2:-false}"
            ;;
    esac
}

get_env_value() {
    local key="$1"
    local line

    line=$(grep -E "^${key}=" "${ENV_FILE}" | tail -1 || true)
    if [ -z "${line}" ]; then
        return 0
    fi

    printf '%s' "${line#*=}"
}

set_env_value() {
    local key="$1"
    local value="$2"
    local escaped_value

    escaped_value=$(printf '%s' "${value}" | sed -e 's/[&|]/\\&/g')

    if grep -q -E "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*|${key}=${escaped_value}|" "${ENV_FILE}"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    fi
}

prompt_input() {
    local prompt="$1"
    local default_value="$2"
    local answer=""

    if ! can_prompt || auto_config; then
        printf '%s' "${default_value}"
        return 0
    fi

    if [ -n "${default_value}" ]; then
        printf '%s [%s]: ' "${prompt}" "${default_value}" > /dev/tty
    else
        printf '%s: ' "${prompt}" > /dev/tty
    fi

    IFS= read -r answer < /dev/tty || true
    if [ -z "${answer}" ]; then
        answer="${default_value}"
    fi

    printf '%s' "${answer}"
}

prompt_bool() {
    local prompt="$1"
    local default_value
    local answer

    default_value=$(normalize_bool "$2" "false")
    if [ "${default_value}" = "true" ]; then
        answer=$(prompt_input "${prompt}" "Y/n")
        case "$(printf '%s' "${answer}" | tr '[:upper:]' '[:lower:]')" in
            ""|y|yes)
                printf 'true'
                ;;
            n|no)
                printf 'false'
                ;;
            *)
                printf '%s' "${default_value}"
                ;;
        esac
    else
        answer=$(prompt_input "${prompt}" "y/N")
        case "$(printf '%s' "${answer}" | tr '[:upper:]' '[:lower:]')" in
            y|yes)
                printf 'true'
                ;;
            ""|n|no)
                printf 'false'
                ;;
            *)
                printf '%s' "${default_value}"
                ;;
        esac
    fi
}

generate_auth_token() {
    od -An -N16 -tx1 /dev/urandom | tr -d ' \n'
}

systemd_user_dir() {
    printf '%s' "${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repo-url)
            REPO_URL="$2"
            shift 2
            ;;
        --ref)
            REF="$2"
            shift 2
            ;;
        --prefix)
            PREFIX="$2"
            shift 2
            ;;
        --bin-dir)
            BIN_DIR="$2"
            shift 2
            ;;
        --state-dir)
            STATE_DIR="$2"
            shift 2
            ;;
        --skip-system-packages)
            SKIP_SYSTEM_PACKAGES="true"
            shift
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            log "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

LAUNCHER_PATH="${BIN_DIR}/${LAUNCHER_NAME}"
ENV_FILE="${PREFIX}/.env"
SERVICE_NAME="${LAUNCHER_NAME}.service"
SERVICE_FILE="$(systemd_user_dir)/${SERVICE_NAME}"

print_plan() {
    log "TeleCLI ${INSTALL_LABEL} install plan"
    log "  Install dir: ${PREFIX}"
    log "  Launcher: ${LAUNCHER_PATH}"
    log "  State dir: ${STATE_DIR}"
    log "  Repo URL: ${REPO_URL}"
    log "  Ref: ${REF}"
    log "  Startup service: ${SERVICE_FILE}"
}

install_system_packages() {
    if [ "${SKIP_SYSTEM_PACKAGES}" = "true" ]; then
        log "Skipping apt package installation"
        return 0
    fi

    if ! command -v apt-get >/dev/null 2>&1 || ! command -v sudo >/dev/null 2>&1; then
        log "Skipping apt package installation because sudo/apt-get is unavailable"
        return 0
    fi

    run_cmd sudo apt-get update
    run_cmd sudo apt-get install -y git python3 python3-venv python3-pip tmux curl
}

sync_repo() {
    if [ -d "${PREFIX}/.git" ]; then
        run_cmd git -C "${PREFIX}" fetch --tags origin
        run_cmd git -C "${PREFIX}" checkout "${REF}"
        if [[ "${REF}" != refs/tags/* ]] && [[ "${REF}" != v* ]]; then
            run_cmd git -C "${PREFIX}" pull --ff-only origin "${REF}"
        fi
        return 0
    fi

    run_cmd git clone --branch "${REF}" --single-branch "${REPO_URL}" "${PREFIX}"
}

setup_python() {
    run_cmd python3 -m venv "${PREFIX}/venv"
    run_cmd "${PREFIX}/venv/bin/pip" install --upgrade pip
    run_cmd "${PREFIX}/venv/bin/pip" install -r "${PREFIX}/requirements.txt"
}

configure_env_file() {
    local telegram_token allowed_users web_host web_port auth_required auth_token
    local ai_proxy_enabled ai_proxy_provider bind_localhost enable_telegram generated_auth_token=""

    telegram_token=$(placeholder_to_blank "${TELECLI_INSTALL_TELEGRAM_BOT_TOKEN:-$(get_env_value TELEGRAM_BOT_TOKEN)}")
    allowed_users="${TELECLI_INSTALL_ALLOWED_TELEGRAM_USERS:-$(get_env_value ALLOWED_TELEGRAM_USERS)}"
    web_host="${TELECLI_INSTALL_WEB_HOST:-$(get_env_value WEB_HOST)}"
    web_port="${TELECLI_INSTALL_WEB_PORT:-$(get_env_value WEB_PORT)}"
    auth_required=$(normalize_bool "${TELECLI_INSTALL_AUTH_REQUIRED:-$(get_env_value AUTH_REQUIRED)}" "true")
    auth_token=$(placeholder_to_blank "${TELECLI_INSTALL_AUTH_TOKEN:-$(get_env_value AUTH_TOKEN)}")
    ai_proxy_enabled=$(normalize_bool "${TELECLI_INSTALL_AI_PROXY_ENABLED:-$(get_env_value AI_PROXY_ENABLED)}" "false")
    ai_proxy_provider="${TELECLI_INSTALL_AI_PROXY_PROVIDER:-$(get_env_value AI_PROXY_PROVIDER)}"
    START_AT_STARTUP=$(normalize_bool "${TELECLI_INSTALL_START_AT_STARTUP:-${START_AT_STARTUP}}" "false")

    [ -n "${web_host}" ] || web_host="127.0.0.1"
    [ -n "${web_port}" ] || web_port="8000"
    [ -n "${ai_proxy_provider}" ] || ai_proxy_provider="gemini-cli"

    if can_prompt && ! auto_config; then
        log "Configuring ${ENV_FILE}"
        enable_telegram="false"
        if [ -n "${telegram_token}" ]; then
            enable_telegram="true"
        fi
        enable_telegram=$(prompt_bool "Enable Telegram bot integration?" "${enable_telegram}")
        if [ "${enable_telegram}" = "true" ]; then
            telegram_token=$(prompt_input "Telegram bot token" "${telegram_token}")
            allowed_users=$(prompt_input "Allowed Telegram user IDs (comma-separated, optional)" "${allowed_users}")
        else
            telegram_token=""
            allowed_users=""
        fi

        bind_localhost="true"
        if [ "${web_host}" = "0.0.0.0" ]; then
            bind_localhost="false"
        fi
        bind_localhost=$(prompt_bool "Bind the web UI to localhost only?" "${bind_localhost}")
        if [ "${bind_localhost}" = "true" ]; then
            web_host="127.0.0.1"
        else
            web_host="0.0.0.0"
        fi

        web_port=$(prompt_input "Web port" "${web_port}")
        auth_required=$(prompt_bool "Require an auth token for web access?" "${auth_required}")
        if [ "${auth_required}" = "true" ]; then
            auth_token=$(prompt_input "Auth token (leave blank to auto-generate)" "${auth_token}")
        else
            auth_token=""
        fi

        ai_proxy_enabled=$(prompt_bool "Enable AI proxy by default?" "${ai_proxy_enabled}")
        if [ "${ai_proxy_enabled}" = "true" ]; then
            ai_proxy_provider=$(prompt_input "AI proxy provider (gemini-cli, claude-cli, github-cli)" "${ai_proxy_provider}")
        fi
        START_AT_STARTUP=$(prompt_bool "Start TeleCLI at startup/login with systemd?" "${START_AT_STARTUP}")
    fi

    auth_required=$(normalize_bool "${auth_required}" "true")
    ai_proxy_enabled=$(normalize_bool "${ai_proxy_enabled}" "false")

    if [ "${auth_required}" = "true" ] && [ -z "${auth_token}" ]; then
        auth_token=$(generate_auth_token)
        generated_auth_token="${auth_token}"
    fi

    if [ "${ai_proxy_enabled}" != "true" ]; then
        ai_proxy_enabled="false"
    fi

    set_env_value TELEGRAM_BOT_TOKEN "${telegram_token}"
    set_env_value TELEGRAM_WEBHOOK_URL ""
    set_env_value ALLOWED_TELEGRAM_USERS "${allowed_users}"
    set_env_value WEB_HOST "${web_host}"
    set_env_value WEB_PORT "${web_port}"
    set_env_value AUTH_REQUIRED "${auth_required}"
    set_env_value AUTH_TOKEN "${auth_token}"
    set_env_value AI_PROXY_ENABLED "${ai_proxy_enabled}"
    set_env_value AI_PROXY_PROVIDER "${ai_proxy_provider}"

    if [ -n "${generated_auth_token}" ]; then
        log "Generated AUTH_TOKEN and stored it in ${ENV_FILE}. Keep this token secret and do not share it."
    fi
}

ensure_env_file() {
    if [ -f "${ENV_FILE}" ]; then
        log "Keeping existing ${ENV_FILE}"
        if is_dry_run; then
            log "[dry-run] chmod 600 ${ENV_FILE}"
            return 0
        fi
        chmod 600 "${ENV_FILE}"
        return 0
    fi

    if is_dry_run; then
        log "[dry-run] cp ${PREFIX}/.env.sample ${ENV_FILE}"
        if auto_config; then
            log "[dry-run] apply TELECLI_INSTALL_* overrides to ${ENV_FILE}"
        fi
        log "[dry-run] chmod 600 ${ENV_FILE}"
        return 0
    fi

    cp "${PREFIX}/.env.sample" "${ENV_FILE}"
    configure_env_file
    chmod 600 "${ENV_FILE}"
}

install_startup_service() {
    local service_content

    if [ "${START_AT_STARTUP}" != "true" ]; then
        return 0
    fi

    service_content=$(cat <<EOF
[Unit]
Description=TeleCLI
After=network-online.target

[Service]
Type=simple
WorkingDirectory=${PREFIX}
ExecStart=${PREFIX}/venv/bin/python -m src.main
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
)

    run_cmd mkdir -p "$(systemd_user_dir)"
    write_text "${SERVICE_FILE}" "${service_content}"

    if is_dry_run; then
        log "[dry-run] systemctl --user daemon-reload"
        log "[dry-run] systemctl enable --user ${SERVICE_NAME}"
        log "[dry-run] systemctl start --user ${SERVICE_NAME}"
        return 0
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        log "systemctl not found; wrote ${SERVICE_FILE} but did not enable startup"
        return 0
    fi

    if ! systemctl --user daemon-reload; then
        log "systemctl --user daemon-reload failed; wrote ${SERVICE_FILE} but did not enable startup"
        return 0
    fi

    if ! systemctl enable --user "${SERVICE_NAME}"; then
        log "systemctl enable --user failed; wrote ${SERVICE_FILE} but did not enable startup"
        return 0
    fi

    if ! systemctl start --user "${SERVICE_NAME}"; then
        log "systemctl start --user failed; wrote ${SERVICE_FILE} but did not start TeleCLI"
        return 0
    fi
}

write_launcher() {
    local launcher_content
    launcher_content=$(cat <<EOF
#!/bin/bash

set -euo pipefail

TELECLI_HOME="${PREFIX}"
STATE_DIR="${STATE_DIR}"
PID_FILE="\${STATE_DIR}/telecli.pid"
LOG_FILE="\${STATE_DIR}/telecli.log"
ENV_FILE="\${TELECLI_HOME}/.env"

mkdir -p "\${STATE_DIR}"

get_web_port() {
    if [ -f "\${ENV_FILE}" ]; then
        local configured
        configured=\$(grep -E '^WEB_PORT=' "\${ENV_FILE}" | tail -1 | cut -d= -f2- || true)
        if [ -n "\${configured}" ]; then
            printf '%s\n' "\${configured}"
            return 0
        fi
    fi

    printf '8000\n'
}

current_pid() {
    if [ -f "\${PID_FILE}" ]; then
        cat "\${PID_FILE}"
    fi
}

is_running() {
    local pid
    pid=\$(current_pid)
    [ -n "\${pid}" ] && kill -0 "\${pid}" >/dev/null 2>&1
}

start() {
    if is_running; then
        printf 'TeleCLI is already running (pid %s)\n' "\$(current_pid)"
        return 0
    fi

    cd "\${TELECLI_HOME}"
    nohup "\${TELECLI_HOME}/venv/bin/python" -m src.main >> "\${LOG_FILE}" 2>&1 &
    local pid=\$!
    printf '%s' "\${pid}" > "\${PID_FILE}"
    printf 'Started TeleCLI (pid %s)\n' "\${pid}"
    printf 'Open http://localhost:%s\n' "\$(get_web_port)"
}

stop() {
    if ! is_running; then
        rm -f "\${PID_FILE}"
        printf 'TeleCLI is not running\n'
        return 0
    fi

    local pid
    pid=\$(current_pid)
    kill "\${pid}"
    rm -f "\${PID_FILE}"
    printf 'Stopped TeleCLI (pid %s)\n' "\${pid}"
}

status() {
    if is_running; then
        printf 'TeleCLI is running (pid %s)\n' "\$(current_pid)"
    else
        printf 'TeleCLI is stopped\n'
    fi

    printf 'URL: http://localhost:%s\n' "\$(get_web_port)"
    printf 'Logs: %s\n' "\${LOG_FILE}"
}

logs() {
    touch "\${LOG_FILE}"
    tail -n 100 -f "\${LOG_FILE}"
}

url() {
    printf 'http://localhost:%s\n' "\$(get_web_port)"
}

case "\${1:-status}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        start
        ;;
    status)
        status
        ;;
    logs)
        logs
        ;;
    url)
        url
        ;;
    *)
        printf 'Usage: ${LAUNCHER_NAME} {start|stop|restart|status|logs|url}\n' >&2
        exit 1
        ;;
esac
EOF
)

    write_text "${LAUNCHER_PATH}" "${launcher_content}"
    run_cmd chmod +x "${LAUNCHER_PATH}"
}

main() {
    print_plan

    run_cmd mkdir -p "$(dirname "${PREFIX}")" "${BIN_DIR}" "${STATE_DIR}"
    install_system_packages
    sync_repo
    setup_python
    ensure_env_file
    write_launcher
    install_startup_service

    log "${INSTALL_LABEL} install complete"
    log "  Start: ${LAUNCHER_PATH} start"
    log "  Status: ${LAUNCHER_PATH} status"
    log "  Logs: ${LAUNCHER_PATH} logs"
}

main
