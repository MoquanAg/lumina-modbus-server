#!/bin/bash
# Lumina Modbus Server Setup Script
# This script sets up the necessary environment and autostart configuration
# for the Lumina Modbus Server application.
# Requires root privileges.

# Check if the script is run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root or using sudo."
  exit 1
fi

echo "Setting up startup scripts."

# Configure Chinese mirrors for better connectivity in China
echo "🇨🇳 Configuring Chinese mirrors for package installation..."
# Backup original sources.list
cp /etc/apt/sources.list /etc/apt/sources.list.backup.$(date +%Y%m%d_%H%M%S) 2>/dev/null || true

# Configure Aliyun mirror for apt (works well in China)
CODENAME=$(lsb_release -cs 2>/dev/null || echo "bookworm")
tee /etc/apt/sources.list > /dev/null << EOF
# Aliyun mirrors for better connectivity in China
deb https://mirrors.aliyun.com/debian/ $CODENAME main contrib non-free non-free-firmware
deb https://mirrors.aliyun.com/debian/ $CODENAME-updates main contrib non-free non-free-firmware
deb https://mirrors.aliyun.com/debian/ $CODENAME-backports main contrib non-free non-free-firmware
deb https://mirrors.aliyun.com/debian-security/ $CODENAME-security main contrib non-free non-free-firmware
EOF

# Fix GPG keys and package verification issues
echo "🔑 Fixing GPG keys and package verification..."
# Fix any broken GPG keys
apt-key adv --keyserver keyserver.ubuntu.com --recv-keys $(apt update 2>&1 | grep -o '[A-F0-9]\{8,\}' | sort -u | tr '\n' ' ') 2>/dev/null || true

# Fix GPG key verification issues that commonly occur
mkdir -p /etc/apt/keyrings
chmod 755 /etc/apt/keyrings

# Update package database with proper error handling
echo "📦 Updating package database..."
apt update --fix-missing || {
    echo "   ⚠️  Initial update failed, trying to fix GPG issues..."
    # Clear apt cache and try again
    apt clean
    rm -rf /var/lib/apt/lists/*
    apt update --allow-unauthenticated || {
        echo "   ⚠️  Still having issues, proceeding with setup..."
    }
}

# Configuration
LUMINA_HOME="/home/lumina"
LUMINA_MODBUS="$LUMINA_HOME/lumina-modbus-server"
VENV_PATH="$LUMINA_MODBUS/venv"
PACKAGE_PATH="$LUMINA_MODBUS/lumina-modbus-server"

# Create directories if they don't exist and set permissions
echo "Creating directories and setting permissions."
sudo -u lumina mkdir -p "$LUMINA_MODBUS"
chown -R lumina:lumina "$LUMINA_HOME"
chmod -R 755 "$LUMINA_HOME"

echo "Setting up Python 3.11 virtual environment."

# Set up Python 3.11 virtual environment
cd "$LUMINA_MODBUS"
sudo -u lumina python3.11 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"
sudo -u lumina "$VENV_PATH/bin/python" -m pip install --upgrade pip

# Configure pip to use Aliyun mirror
echo "Configuring pip to use Aliyun mirror."
sudo -u lumina "$VENV_PATH/bin/pip" config set global.index-url https://mirrors.aliyun.com/pypi/simple/
sudo -u lumina "$VENV_PATH/bin/pip" config set global.trusted-host mirrors.aliyun.com

# Install requirements using Aliyun mirror
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
