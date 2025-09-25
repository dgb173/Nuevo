#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

py -m playwright install --with-deps
