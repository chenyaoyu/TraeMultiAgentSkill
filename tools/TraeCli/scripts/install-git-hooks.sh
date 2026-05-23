#!/bin/sh

set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

git -C "$repo_root" config core.hooksPath .githooks
git -C "$repo_root" config commit.template .gitmessage.txt

printf '%s\n' \
  "Configured repository Git settings:" \
  "  core.hooksPath=.githooks" \
  "  commit.template=.gitmessage.txt" \
  "" \
  "Commit header format:" \
  "  <type>(<scope>): <subject>"
