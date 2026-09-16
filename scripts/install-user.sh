#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_path="$project_root/.venv"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
service_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

python3 -m venv "$venv_path"
"$venv_path/bin/pip" install "setuptools>=68" "websockets>=14,<17"
"$venv_path/bin/pip" install --no-build-isolation --no-deps --editable "$project_root"

mkdir -p "$bin_dir" "$service_dir"
ln -sfn "$venv_path/bin/wispr" "$bin_dir/wispr"

sed \
  -e "s|@PROJECT_ROOT@|$project_root|g" \
  -e "s|@WISPR_BIN@|$venv_path/bin/wispr|g" \
  "$project_root/systemd/wispr.service.in" \
  > "$service_dir/wispr.service"

systemctl --user daemon-reload
systemctl --user enable --now wispr.service

printf 'Installed wispr at %s\n' "$bin_dir/wispr"
printf 'Next: bind a GNOME keyboard shortcut to: %s toggle\n' "$bin_dir/wispr"

