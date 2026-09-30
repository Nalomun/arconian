#!/usr/bin/env bash
cd "$(dirname "$0")"
mkdir -p logs
exec .venv/bin/python main.py >> logs/arconian_stdout.log 2>&1
