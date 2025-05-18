#!/bin/bash

# Get the directory where the script is located
SCRIPT_DIR="$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" && pwd)"
# Assuming install_service.sh is in the root of the 'literallybot' directory structure
BOT_INSTALL_FOLDER="$SCRIPT_DIR"

# Prompt for user and group if not provided as arguments
read -p "Enter the username for the service (default: $(whoami)): " SERVICE_USER
SERVICE_USER=${SERVICE_USER:-$(whoami)}

read -p "Enter the group for the service (default: $(id -gn)): " SERVICE_GROUP
SERVICE_GROUP=${SERVICE_GROUP:-$(id -gn)}

# Python interpreter path (adjust if your python3 is in a different location)
PYTHON_EXEC=$(which python3)
if [ -z "$PYTHON_EXEC" ]; then
    echo "Error: python3 not found in PATH. Please install Python 3 or adjust PYTHON_EXEC in this script."
    exit 1
fi

# Path to the bot.py script
BOT_SCRIPT_PATH="$BOT_INSTALL_FOLDER/bot.py"

# Create the systemd service file content
SERVICE_FILE_CONTENT="[Unit]
Description=Literally a Discord Bot
Wants=network-online.target
After=network.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$BOT_INSTALL_FOLDER/
ExecStart=$PYTHON_EXEC $BOT_SCRIPT_PATH
ExecStop=/usr/bin/pkill -9 -f bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"

# Define the path for the new systemd service file
SERVICE_FILE_PATH="/etc/systemd/system/literallybot.service"

# Create the service file
echo "Creating systemd service file at $SERVICE_FILE_PATH..."
# Use sudo tee to write the file as root
echo -e "$SERVICE_FILE_CONTENT" | sudo tee "$SERVICE_FILE_PATH" > /dev/null

if [ $? -ne 0 ]; then
    echo "Error: Failed to create service file. Make sure you have sudo privileges."
    exit 1
fi

echo "Service file created successfully."

# Reload systemd daemon, enable and start the service
echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling literallybot service to start on boot..."
sudo systemctl enable literallybot.service

echo "Starting literallybot service..."
sudo systemctl start literallybot.service

echo ""
echo "LiterallyBot service setup complete."
echo "You can check the status with: sudo systemctl status literallybot.service"
echo "And view logs with: sudo journalctl -u literallybot.service -f"
