#!/bin/bash

# Check if 1Password CLI is installed
if ! command -v op &> /dev/null; then
    echo "Error: 1Password CLI not found. Please install it first." >&2
    exit 1
fi

# Function to print usage information
print_usage() {
    echo "Usage: $0 --vault-id <vault-id>" >&2
    exit 1
}

# Check for required arguments
if [ "$#" -ne 2 ]; then
    print_usage
fi

if [ "$1" != "--vault-id" ]; then
    print_usage
fi

VAULT_ID="$2"

# Check if VAULT_ID is empty
if [ -z "$VAULT_ID" ]; then
    echo "Error: vault-id cannot be empty" >&2
    exit 1
fi

# Check if connected to 1Password
if ! op account get &> /dev/null; then
    echo "Error: Not signed in to 1Password. Please run 'op signin' first." >&2
    exit 1
fi

# Print notice to TTY
echo "Fetching secret from 1Password..." >/dev/tty

# Attempt to read the secret
if ! SECRET=$(op read "$VAULT_ID"); then
    echo "Error: Failed to read secret from 1Password" >&2
    exit 1
fi

# Output the secret to stdout
echo "$SECRET"