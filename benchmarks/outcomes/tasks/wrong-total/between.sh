#!/bin/bash
# The production sample and the replay tool are taken away between the sessions.
set -e
cd "$1"
rm -rf data tools
