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

# Set up systemd service for Lumina Modbus Server
cat << EOF > /etc/systemd/system/lumina-modbus.service
[Unit]
Description=Lumina Modbus Server
After=network.target

[Service]
ExecStart=/home/lumina/lumina-modbus-server/start_modbus_server.sh
User=lumina
Group=lumina

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the systemd service
sudo systemctl enable lumina-modbus.service
sudo systemctl start lumina-modbus.service

wait

echo "Setup complete."
