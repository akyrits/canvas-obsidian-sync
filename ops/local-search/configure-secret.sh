#!/usr/bin/env bash
set -euo pipefail

settings_path="${1:?settings path is required}"
placeholder="__SEARXNG_SECRET__"

if ! grep --quiet --fixed-strings "${placeholder}" "${settings_path}"; then
  echo "SearXNG secret placeholder is missing; refusing to overwrite settings." >&2
  exit 1
fi

secret="$(openssl rand -hex 32)"
sed --in-place "s/${placeholder}/${secret}/" "${settings_path}"
chmod 0640 "${settings_path}"

if grep --quiet --fixed-strings "${placeholder}" "${settings_path}"; then
  echo "SearXNG secret replacement failed." >&2
  exit 1
fi

echo "SearXNG secret generated without exposing it."
