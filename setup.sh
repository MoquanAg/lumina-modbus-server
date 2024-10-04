#!/bin/bash

# Check if the script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root or using sudo."
  exit 1
fi

echo "Setting up startup scripts."

# Configuration
LUMINA_HOME="/home/lumina"
LUMINA_MODBUS="$LUMINA_HOME/lumina-modbus-server"
VENV_PATH="$LUMINA_MODBUS/venv"
PACKAGE_PATH="$LUMINA_MODBUS/lumina-modbus-server"

echo "Setting up Python 3.11 virtual environment."

# Set up Python 3.11 virtual environment
sudo -u lumina python3.11 -m venv "$VENV_PATH"
sudo -u lumina "$VENV_PATH/bin/pip" install --upgrade pip
sudo -u lumina "$VENV_PATH/bin/pip" install -r "$LUMINA_MODBUS/requirements.txt"

# Create autostart entry for lxterminal
AUTOSTART_DIR="/home/lumina/.config/autostart"
sudo -u lumina mkdir -p "$AUTOSTART_DIR"

cat << EOF | sudo -u lumina tee "$AUTOSTART_DIR/lumina-modbus-terminal.desktop" > /dev/null
[Desktop Entry]
Type=Application
Name=Lumina Modbus Terminal
Exec=lxterminal -e "bash -c '/home/lumina/lumina-modbus-server/start_modbus_server.sh; exec bash'"
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo "Added lxterminal autostart entry for Lumina Modbus."

echo "Setup complete."
