#!/bin/sh
# MODE=restore|promote
# DEPLOY_PACKAGE: empty | all-packages | <technical-name>
set -eu

mode="${MODE:?MODE is required}"
packages_file="${PACKAGES_FILE:-packages.json}"
choice="${DEPLOY_PACKAGE:-}"
root="${CI_PROJECT_DIR:-.}"
cd "$root"

all_packages() {
  python3 -c "import json; print('\\n'.join(json.load(open('${packages_file}'))['packages']))"
}

if [ "$choice" = "all-packages" ]; then
  package_list="$(all_packages)"
elif [ -n "$choice" ]; then
  package_list="$choice"
elif [ "$mode" = "restore" ]; then
  echo "Input deploy_package is empty. Refusing to deploy."
  exit 0
else
  echo "Promote without package selection is not implemented yet."
  exit 1
fi

echo "Packages:"
printf '%s\n' "$package_list"

while IFS= read -r pkg; do
  [ -n "$pkg" ] || continue
  echo "======== Upload package: $pkg ========"
  sap-pipe sync \
    --package-id "$pkg" \
    --dir-git-repo "$root" \
    --dir-artifacts "$root/$pkg" \
    --target tenant
done <<EOF
$package_list
EOF
