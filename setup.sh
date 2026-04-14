#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "=== CB Stream Saver — Setup ==="

# Check uv
if ! command -v uv &>/dev/null; then
    echo "Error: uv is required but not found."
    echo "  Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "✓ uv $(uv --version | awk '{print $2}') found"

# Check Python version
if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
    echo "Error: Python 3.10+ is required."
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "✓ Python $PYTHON_VERSION found"

# Sync dependencies with uv
echo "Syncing dependencies..."
uv sync

# Check ffmpeg
if command -v ffmpeg &>/dev/null; then
    echo "✓ ffmpeg found"
else
    echo "⚠ ffmpeg not found. MP4 conversion will not work."
    echo "  Install: sudo pacman -S ffmpeg  (Arch)"
    echo "           sudo apt install ffmpeg  (Debian/Ubuntu)"
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "To run the server:"
echo "  uv run python app.py"
echo ""
echo "Then open http://localhost:8000 in your browser."
