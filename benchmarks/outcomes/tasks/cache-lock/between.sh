#!/bin/bash
# The stress tool and any scratch copies are cleared away between the sessions.
set -e
cd "$1"
rm -rf tools scratch kvcache_scratch
