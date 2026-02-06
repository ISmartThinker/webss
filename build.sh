#!/bin/bash
set -e

echo "Installing Playwright browsers..."
python -m playwright install chromium --with-deps

echo "Build complete!"
