#!/usr/bin/env bash
set -e

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Running pytest with coverage..."
pytest --cov=LuminaLogger \
       --cov-report=term \
       --cov-report=html \
       tests/
       chmod +x run_tests.sh