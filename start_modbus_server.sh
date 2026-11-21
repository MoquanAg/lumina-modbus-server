#!/bin/bash
# Lumina Modbus Server Startup Script
# This script manages the startup of the Modbus server application.
#
# Start-if-down by default: a healthy server makes this a no-op. --restart forces
# the old kill-and-relaunch behavior. Without a display (or without lxterminal)
# the server runs in a detached tmux session `lumina-modbus`, so the cron stack
# watchdog can start it without a desktop session.

VENV_PATH="/home/lumina/lumina-modbus-server/venv"
PACKAGE_PATH="/home/lumina/lumina-modbus-server"

RESTART=0
for arg in "$@"; do
    case "$arg" in
        --restart) RESTART=1 ;;
        *) echo "Unknown argument: $arg (only --restart is supported)" >&2; exit 2 ;;
    esac
done

modbus_alive() {
    pgrep -af 'lumina-modbus-server/main\.py' | grep -E '(^|[ /])python[0-9.]* ' | grep -q .
}

if [ "$RESTART" -ne 1 ] && modbus_alive; then
    echo "Lumina Modbus Server already running; nothing to do (use --restart to bounce)."
    exit 0
fi

if [ -z "$DISPLAY" ] || ! command -v lxterminal >/dev/null 2>&1; then
    # Headless path (cron watchdog / no desktop session).
    if tmux has-session -t lumina-modbus 2>/dev/null; then
        if [ "$RESTART" -eq 1 ] || ! modbus_alive; then
            tmux kill-session -t lumina-modbus
        else
            exit 0
        fi
    fi
    tmux new-session -d -s lumina-modbus -c "$PACKAGE_PATH" \
        "$VENV_PATH/bin/python" "$PACKAGE_PATH/main.py"
    echo "Lumina Modbus Server launched in tmux session lumina-modbus (headless)."
    exit 0
fi

# GUI path (desktop autostart).
start_script() {
    local script_name=$1
    lxterminal -e "bash -c '
        source $VENV_PATH/bin/activate
        echo Starting $script_name...
        python $PACKAGE_PATH/$script_name
        exec bash
    '" &
}

pkill -f "$PACKAGE_PATH/main.py"
sleep 2
start_script "main.py"
echo "Lumina Modbus Server has been launched."
