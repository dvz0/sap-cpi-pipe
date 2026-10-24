#!/bin/sh
# MODE=restore  → deploy_dev. Empty deploy_package does nothing.
# MODE=promote  → deploy_qas / deploy_prod. Empty deploy_package deploys
#                 only packages whose files changed in this push.
# DEPLOY_PACKAGE: empty | all-packages | <technical-name>
set -eu

mode="${MODE:?MODE is required}"
packages_file="${PACKAGES_FILE:-packages.json}"
choice="${DEPLOY_PACKAGE:-}"
root="${CI_PROJECT_DIR:-.}"
cd "$root"

all_packages() {
  python3 -c "import json; print('\n'.join(json.load(open('${packages_file}'))['packages']))"
}

bundle_type_of() {
  awk '
    /^SAP-BundleType:[[:space:]]*/ {
      sub(/^SAP-BundleType:[[:space:]]*/, "")
      sub(/\r$/, "")
      sub(/[[:space:]]+$/, "")
      print
      exit
    }
  ' "$1"
}

# CPI deploy of a value mapping returns HTTP 500 when an entry value is empty.
# Skip that artifact. The rest of the package still deploys, and the job stays green.
value_mapping_deployable() {
  xml="$1value_mapping.xml"
  artifact_id="$2"
  [ -f "$xml" ] || return 0
  python3 - "$xml" "$artifact_id" << 'PY' || return 1
import sys
import xml.etree.ElementTree as ET

path, artifact_id = sys.argv[1], sys.argv[2]
try:
    root = ET.parse(path).getroot()
except ET.ParseError as exc:
    sys.stderr.write(f"Skipping value mapping {artifact_id}: value_mapping.xml is not valid XML: {exc}\n")
    sys.exit(1)
empty = []
for entry in root.iter("entry"):
    value = entry.find("value")
    text = "" if value is None or value.text is None else value.text.strip()
    if text:
        continue
    agency = (entry.findtext("agency") or "").strip()
    schema = (entry.findtext("schema") or "").strip()
    empty.append(f"agency={agency} schema={schema}")
if empty:
    listed = "; ".join(empty)
    sys.stderr.write(
        f"Skipping value mapping {artifact_id}: empty <value> in value_mapping.xml ({listed}). "
        "CPI deploy returns HTTP 500 for this content.\n"
    )
    sys.exit(1)
PY
}

# IntegrationFlow is the only bundle type whose API name differs.
# DataType, MessageType and FaultMessageType have no runtime deploy.
deploy_type_of() {
  case "$1" in
    IntegrationFlow) printf '%s\n' Integration ;;
    ScriptCollection|ValueMapping|MessageMapping) printf '%s\n' "$1" ;;
    DataType|MessageType|FaultMessageType) printf '%s\n' skip ;;
    "") printf '%s\n' missing ;;
    *) printf '%s\n' unsupported ;;
  esac
}

changed_packages() {
  before="${CI_COMMIT_BEFORE_SHA:-}"
  after="${CI_COMMIT_SHA:-HEAD}"
  zeros="0000000000000000000000000000000000000000"
  if [ -z "$before" ] || [ "$before" = "$zeros" ]; then
    before="$(git rev-parse HEAD^ 2>/dev/null || true)"
  fi
  if [ -z "$before" ]; then
    echo "Cannot see the previous commit. Re-run with deploy_package=all-packages or a package id." >&2
    exit 1
  fi
  if ! git cat-file -e "${before}^{commit}" 2>/dev/null; then
    git fetch --deepen=50 origin "${CI_COMMIT_REF_NAME:-HEAD}" >/dev/null 2>&1 || true
  fi
  if ! git cat-file -e "${before}^{commit}" 2>/dev/null; then
    echo "Previous commit $before is not in this clone. Re-run with deploy_package set explicitly." >&2
    exit 1
  fi
  git diff --name-only "$before" "$after" | BEFORE_SHA="$before" PACKAGES_FILE_PATH="$packages_file" python3 -c '
import json, os, subprocess, sys
packages_file = os.environ["PACKAGES_FILE_PATH"]
before = os.environ["BEFORE_SHA"]
known = json.load(open(packages_file))["packages"]
changed = set()
manifest = False
manifest_path = os.path.relpath(os.path.abspath(packages_file), os.getcwd())
for line in sys.stdin:
    path = line.strip()
    if not path:
        continue
    if path == manifest_path:
        manifest = True
        continue
    top = path.split("/", 1)[0]
    if top in known:
        changed.add(top)
if manifest:
    rel = os.path.relpath(packages_file, os.getcwd())
    old = None
    try:
        text = subprocess.check_output(["git", "show", f"{before}:{rel}"], text=True, stderr=subprocess.DEVNULL)
        old = json.loads(text).get("packages") or []
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        print("packages.json has no previous revision; not selecting every package.", file=sys.stderr)
    if old is not None:
        previous = set(old)
        added = [pkg for pkg in known if pkg not in previous]
        changed.update(added)
        if not added and not changed:
            print("packages.json changed but no package was added. Nothing new to deploy.", file=sys.stderr)
print("\n".join(pkg for pkg in known if pkg in changed))
'
}

if [ "$choice" = "all-packages" ]; then
  echo "deploy_package=all-packages"
  package_list="$(all_packages)"
elif [ -n "$choice" ]; then
  echo "deploy_package=$choice"
  package_list="$choice"
elif [ "$mode" = "restore" ]; then
  echo "Input deploy_package is empty. Refusing to deploy."
  echo "Set it to a package technical name, or to all-packages."
  exit 0
else
  echo "deploy_package empty → only packages changed in this push"
  package_list="$(changed_packages)"
fi

if [ -z "$package_list" ]; then
  echo "No packages selected. Nothing to deploy."
  exit 0
fi

echo "Packages:"
printf '%s\n' "$package_list"

list_file="$(mktemp)"
ids_file="$(mktemp)"
redeploy_file="$(mktemp)"
: > "$redeploy_file"
printf '%s\n' "$package_list" > "$list_file"
found_manifest=0

while IFS= read -r pkg; do
  [ -n "$pkg" ] || continue
  echo "======== Upload package: $pkg ========"
  part="$(mktemp)"
  sap-pipe sync \
    --package-id "$pkg" \
    --dir-git-repo "$root" \
    --dir-artifacts "$root/$pkg" \
    --target tenant \
    --redeploy-file "$part"
  if [ -s "$part" ]; then
    cat "$part" >> "$redeploy_file"
  fi
  rm -f "$part"
  for dir in "$root/$pkg"/*/; do
    [ -d "$dir" ] || continue
    manifest="${dir}META-INF/MANIFEST.MF"
    [ -f "$manifest" ] || continue
    found_manifest=1
    artifact_id="$(basename "$dir")"
    bundle_type="$(bundle_type_of "$manifest")"
    deploy_type="$(deploy_type_of "$bundle_type")"
    case "$deploy_type" in
      skip)
        echo "Skipping $artifact_id ($bundle_type): runtime deploy is not supported"
        ;;
      missing)
        echo "SAP-BundleType missing in $manifest" >&2
        exit 1
        ;;
      unsupported)
        echo "Unsupported SAP-BundleType '$bundle_type' in $manifest" >&2
        exit 1
        ;;
      *)
        if [ "$deploy_type" = "ValueMapping" ] && ! value_mapping_deployable "$dir" "$artifact_id"; then
          continue
        fi
        printf '%s\t%s\n' "$deploy_type" "$artifact_id" >> "$ids_file"
        ;;
    esac
  done
done < "$list_file"

if [ ! -s "$ids_file" ]; then
  if [ "$found_manifest" -eq 0 ]; then
    echo "No artifacts with META-INF/MANIFEST.MF found; nothing to deploy"
  else
    echo "No deployable artifacts found; nothing to deploy"
  fi
  exit 0
fi

for artifact_type in ScriptCollection ValueMapping MessageMapping Integration; do
  ids="$(awk -F '\t' -v type="$artifact_type" '
    $1 == type {
      printf "%s%s", sep, $2
      sep = ","
    }
    END { if (sep != "") print "" }
  ' "$ids_file")"
  [ -n "$ids" ] || continue
  echo "======== Deploy $artifact_type artifacts: $ids ========"
  if [ -s "$redeploy_file" ]; then
    force="$(paste -sd, "$redeploy_file")"
    sap-pipe deploy --artifact-type "$artifact_type" --artifact-ids "$ids" --redeploy-ids "$force"
  else
    sap-pipe deploy --artifact-type "$artifact_type" --artifact-ids "$ids"
  fi
done
