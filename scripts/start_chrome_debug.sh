#!/bin/bash
# Start Chrome with CDP enabled for ORACLE browser automation
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=$HOME/.hermes/chrome-debug \
  --no-first-run \
  --no-default-browser-check \
  --disable-extensions \
  &
echo "Chrome started with CDP on port 9222"
echo "Connect Hermes with: /browser connect"
