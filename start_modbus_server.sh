#!/bin/bash

# Configuration
VENV_PATH="/home/lumina/lumina-modbus-server/venv"
PACKAGE_PATH="/home/lumina/lumina-modbus-server"

# Function to start a script in a new terminal
start_script() {
    local script_name=$1
    lxterminal -e "bash -c '
        source $VENV_PATH/bin/activate
        echo Starting $script_name...
        python $PACKAGE_PATH/$script_name
        exec bash
    '" &
}

# Kill existing Lumina Modbus Server processes
pkill -f "$PACKAGE_PATH"

# Start Modbus server script
start_script "lumina_modbus_server.py"

# Start main logic script (if applicable)
# start_script "main.py"

echo "Lumina Modbus Server has been launched."