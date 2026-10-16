#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

while IFS= read -r playbook; do
    ansible-playbook -i inventories/ci/hosts.yml --syntax-check "$playbook"
done < <(find playbooks -type f -name '*.yml' | sort)
