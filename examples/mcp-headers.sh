#!/bin/sh
# Claude Code headersHelper for easysmart-mcp on macOS.
# Prints the Authorization header, reading the token from the login Keychain,
# so the token never sits in a Claude Code config file.
#
# Store the token once (paste it at the hidden prompt):
#   security add-generic-password -a easysmart-mcp -s easysmart-mcp-token -w
t=$(security find-generic-password -a easysmart-mcp -s easysmart-mcp-token -w 2>/dev/null) || {
  echo "easysmart-mcp token not found in Keychain" >&2; exit 1; }
printf '{"Authorization": "Bearer %s"}\n' "$t"
