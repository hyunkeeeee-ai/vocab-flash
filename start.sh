#!/bin/bash
set -e

cd "$(dirname "$0")"
echo "🚀  Starting Vocab Flash on http://localhost:5001"
echo "    Open your browser at: http://localhost:5001"
python3 app.py
