#!/bin/bash

# --- Anilix Universal Setup Script ---
# Supports Linux and macOS
# Usage: ./setup.sh [install|uninstall]

set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
COMMAND_PATH="$BIN_DIR/anilix"

# OS Detection
OS_TYPE="$(uname -s)"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color

display_help() {
    echo "Usage: ./setup.sh {install|uninstall}"
    exit 1
}

if [[ $# -eq 0 ]]; then
    display_help
fi

case "$1" in
        echo -e "${MAGENTA}🚀 Installing Anilix command for $OS_TYPE${NC}"
        
        # Ensure uv is installed
        if ! command -v uv &> /dev/null; then
            echo -e "${RED}❌ Error: 'uv' is not installed. Please install it first from https://github.com/astral-sh/uv${NC}"
            exit 1
        fi

        # Sync dependencies
        echo -e "${CYAN}📦 Syncing dependencies with uv${NC}"
        cd "$APP_DIR" && uv sync
        
        # Ensure bin directory exists
        mkdir -p "$BIN_DIR"
        
        # Create the wrapper script
        cat <<EOF > "$COMMAND_PATH"
#!/bin/bash
# Anilix Global Wrapper
cd "$APP_DIR" && uv run anilix.py "\$@"
EOF
        
        # Make it executable
        chmod +x "$COMMAND_PATH"
        
        echo -e "${GREEN}✅ Success! Command 'anilix' installed to $BIN_DIR${NC}"
        echo ""
        echo -e "${YELLOW}💡 To use it, make sure $BIN_DIR is in your PATH.${NC}"
        if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
            echo -e "   ${RED}⚠️ Warning: $BIN_DIR is not in your current PATH.${NC}"
            echo "   Add this line to your ~/.bashrc or ~/.zshrc:"
            echo "   export PATH=\"\$HOME/.local/bin:\$PATH\""
        fi
        echo ""
        echo -e "${GREEN}🔥 You can now launch Anilix from anywhere by typing: anilix${NC}"
        ;;
        
    uninstall)
        echo -e "${RED}🗑️  Uninstalling Anilix command${NC}"
        if [ -f "$COMMAND_PATH" ]; then
            rm "$COMMAND_PATH"
            echo -e "${GREEN}✅ Successfully uninstalled from $BIN_DIR${NC}"
        else
            echo -e "${YELLOW}❓ Anilix command not found in $BIN_DIR${NC}"
        fi
        ;;
        
    *)
        display_help
        ;;
esac
