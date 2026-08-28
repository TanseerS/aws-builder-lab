#!/usr/bin/env bash
#
# Verify and package the OpsPilot Lambda functions.
#
# Packaging during `terraform apply` is handled by the archive provider, so this
# script is NOT required for deployment. It exists to validate packaging in CI
# and to let you inspect the artifacts Terraform will upload.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT}/terraform/.build"
PYTHON="${PYTHON:-python3}"

FUNCTIONS=(
  api
  incident_detector
  investigator
  remediation
  verifier
  postmortem
  demo_app
  demo_controller
  traffic_generator
)

green() { printf '\033[32m%s\033[0m\n' "$*"; }
red()   { printf '\033[31m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m==>\033[0m %s\n' "$*"; }

info "Checking Python syntax"
failed=0
while IFS= read -r file; do
  if ! "${PYTHON}" -m py_compile "${file}" 2>/dev/null; then
    red "  syntax error: ${file#"${ROOT}"/}"
    failed=1
  fi
done < <(find "${ROOT}/lambda" -name '*.py' -not -path '*/__pycache__/*')

if [[ "${failed}" -ne 0 ]]; then
  red "Python syntax check failed."
  exit 1
fi
green "  all Python files compile"

info "Verifying handler entry points"
for fn in "${FUNCTIONS[@]}"; do
  handler="${ROOT}/lambda/${fn}/handler.py"
  if [[ ! -f "${handler}" ]]; then
    red "  missing handler: lambda/${fn}/handler.py"
    exit 1
  fi
  if ! grep -q '^def lambda_handler' "${handler}"; then
    red "  lambda/${fn}/handler.py has no lambda_handler entry point"
    exit 1
  fi
done
green "  ${#FUNCTIONS[@]} handlers present"

info "Verifying the shared layer layout"
if [[ ! -f "${ROOT}/lambda/shared/python/opspilot/__init__.py" ]]; then
  red "  shared layer must live at lambda/shared/python/opspilot/"
  exit 1
fi
green "  shared layer layout is correct (python/opspilot -> /opt/python/opspilot)"

info "Building zip artifacts into ${BUILD_DIR#"${ROOT}"/}"
mkdir -p "${BUILD_DIR}"
rm -f "${BUILD_DIR}"/*.zip

(
  cd "${ROOT}/lambda/shared"
  zip -qr "${BUILD_DIR}/shared_layer.zip" python -x '*__pycache__*' -x '*.pyc'
)
for fn in "${FUNCTIONS[@]}"; do
  (
    cd "${ROOT}/lambda/${fn}"
    zip -qr "${BUILD_DIR}/${fn}.zip" . -x '*__pycache__*' -x '*.pyc'
  )
done

green "  packaged $(( ${#FUNCTIONS[@]} + 1 )) archives"
info "Artifacts"
ls -lh "${BUILD_DIR}"/*.zip | awk '{printf "    %-10s %s\n", $5, $NF}'

green "Packaging verification complete."
echo
echo "Note: 'terraform apply' repackages these itself via the archive provider."
