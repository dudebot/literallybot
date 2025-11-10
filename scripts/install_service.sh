#!/bin/bash
# Systemd service installer for LiterallyBot Discord Bot

set -e

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if script is run with sudo/root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Error: This script must be run with sudo${NC}"
    echo "Usage: sudo ./install_service.sh [service_name]"
    echo "If no service name is provided, defaults to 'literallybot'"
    exit 1
fi

# Get service name from argument or use default
SERVICE_NAME="${1:-literallybot}"
DESCRIPTION="LiterallyBot Discord Bot"

# Detect current directory and user
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURRENT_USER="${SUDO_USER:-$USER}"

# Check if venv exists, use it if available
if [ -f "$CURRENT_DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$CURRENT_DIR/venv/bin/python3"
    echo -e "${YELLOW}Note: Using virtual environment at $CURRENT_DIR/venv${NC}"
else
    PYTHON_BIN="$(which python3)"
    echo -e "${YELLOW}Note: No venv found, using system python3${NC}"
fi

# Verify bot.py exists
if [ ! -f "$CURRENT_DIR/bot.py" ]; then
    echo -e "${RED}Error: bot.py not found in $CURRENT_DIR${NC}"
    echo "Make sure you're running this script from the bot's scripts/ directory"
    exit 1
fi

# Display configuration
echo -e "${GREEN}=== Service Installation Configuration ===${NC}"
echo "Service name:    $SERVICE_NAME"
echo "Description:     $DESCRIPTION"
echo "Working dir:     $CURRENT_DIR"
echo "Python binary:   $PYTHON_BIN"
echo "Run as user:     $CURRENT_USER"
echo ""

# Ask for confirmation
read -p "Install service with these settings? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Installation cancelled."
    exit 0
fi

# Create systemd service file
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo -e "${YELLOW}Creating service file: $SERVICE_FILE${NC}"

cat > "$SERVICE_FILE" << EOF
[Unit]
Description=$DESCRIPTION
Wants=network-online.target
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
Group=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
ExecStart=$PYTHON_BIN $CURRENT_DIR/bot.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}Service file created successfully${NC}"

# Reload systemd daemon
echo -e "${YELLOW}Reloading systemd daemon...${NC}"
systemctl daemon-reload

# Enable service
echo -e "${YELLOW}Enabling service...${NC}"
systemctl enable "$SERVICE_NAME"

echo ""
echo -e "${GREEN}=== Installation Complete ===${NC}"
echo ""
echo "Service '$SERVICE_NAME' has been installed and enabled."
echo ""

# Start the service
echo -e "${YELLOW}Starting service...${NC}"
systemctl start "$SERVICE_NAME"

# Wait a moment and check status
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo -e "${GREEN}✓ Service started successfully!${NC}"
else
    echo -e "${RED}⚠ Service failed to start. Check logs with:${NC}"
    echo "  sudo journalctl -u $SERVICE_NAME -n 50"
fi

echo ""
echo "Useful commands:"
echo "  sudo systemctl stop $SERVICE_NAME      # Stop the bot"
echo "  sudo systemctl restart $SERVICE_NAME   # Restart the bot"
echo "  sudo systemctl status $SERVICE_NAME    # Check bot status"
echo "  sudo journalctl -u $SERVICE_NAME -f    # View live logs"
echo "  sudo journalctl -u $SERVICE_NAME -n 50 # View last 50 log lines"
echo ""
echo "The service will automatically start on system boot."
