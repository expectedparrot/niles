#!/usr/bin/env bash
set -euo pipefail

target=${1:-hutz-law-demo}
niles_python=${NILES_PYTHON:-python3}

mkdir -p "$target"
cd "$target"

if [[ -d .niles ]]; then
  echo "Refusing to overwrite an existing Niles CRM: $PWD" >&2
  exit 2
fi

niles() {
  "$niles_python" -m niles "$@"
}

quiet_niles() {
  niles "$@" >/dev/null
}

quiet_niles init
quiet_niles org context set \
  "Lionel Hutz represents Springfield clients with enthusiasm and inconsistent paperwork." \
  --name "Hutz Law" \
  --trait reporting_currency=USD

quiet_niles teammate add "Lionel Hutz" --alias lionel --role attorney
quiet_niles teammate add "Cookie Kwan" --alias cookie --role referrals

# Deals closest to revenue.
quiet_niles contact add "Burns Industries" --tag prospect \
  --trait stage=contracting --trait priority=1 \
  --trait deal_value=120000 --trait expected_mrr=10000 --cadence-days 7
quiet_niles contact add "Waylon Smithers" --company "Burns Industries" --role champion
quiet_niles note add burns-industries "Waiting on Mr. Burns to return the engagement letter." --kind meeting --at 2026-08-27
quiet_niles task add burns-industries "Ask Smithers for signature timing" --assign lionel --due 2026-09-03 --tag contracting

quiet_niles contact add "Globex Corporation" --tag prospect \
  --trait stage=contracting --trait priority=1 \
  --trait deal_value=90000 --trait expected_mrr=7500 --cadence-days 7
quiet_niles contact add "Hank Scorpio" --company "Globex Corporation" --role decision-maker
quiet_niles note add globex-corporation "Retainer terms agreed; signature is the remaining step." --kind call --at 2026-08-30
quiet_niles task add globex-corporation "Send final retainer packet" --assign cookie --due 2026-09-04 --tag contracting

quiet_niles contact add "Springfield Monorail Authority" --tag prospect \
  --trait stage=pilot --trait priority=2 \
  --trait deal_value=60000 --trait expected_mrr=5000 --cadence-days 10
quiet_niles contact add "Lyle Lanley" --company "Springfield Monorail Authority" --role champion
quiet_niles note add springfield-monorail-authority "Negotiating an investigative review of the monorail contract." --kind meeting --at 2026-08-25
quiet_niles task add springfield-monorail-authority "Return scoped investigation proposal" --assign lionel --due 2026-09-06 --tag pilot

# A stalled dependency.
quiet_niles contact add "The Leftorium" --tag prospect \
  --trait stage=stalled --trait priority=2 \
  --trait deal_value=36000 --trait expected_mrr=3000 --cadence-days 14
quiet_niles contact add "Ned Flanders" --company "The Leftorium" --role owner
quiet_niles note add the-leftorium "Waiting on the mall to approve a lease amendment." --kind call --at 2026-08-14
quiet_niles task add the-leftorium "Draft a temporary kiosk workaround" --assign cookie --due 2026-09-05 --tag unblock

# A warm-introduction target.
quiet_niles contact add "Springfield Nuclear Power Plant" --tag target \
  --trait stage=target --trait priority=3 --trait connector=Smithers
quiet_niles contact add "Waylon Smithers" --company "Springfield Nuclear Power Plant" --role introducer \
  --email smithers@burns.example
quiet_niles note add springfield-nuclear-power-plant "Smithers offered to make an introduction to the innovation team." --kind call --at 2026-08-21
quiet_niles task add springfield-nuclear-power-plant "Send Smithers a forwardable introduction" --assign cookie --due 2026-09-07 --tag warm-intro

# Closed-out accounts should never pollute the active pipeline.
quiet_niles contact add "Krustylu Studios" --tag lost --trait stage=lost \
  --trait current_status="No active path after evaluation"
quiet_niles contact add "Duff Brewery" --tag lost --trait stage=lost
quiet_niles contact add "Itchy and Scratchy Studios" --tag lost --trait stage=lost
quiet_niles contact add "Shelbyville Nuclear" --tag lost --trait stage=lost
quiet_niles contact add "Springfield Republican Party" --tag lost --trait stage=lost

# Intentionally incomplete records exercise cleanup warnings.
quiet_niles contact add "Olivia" --company "Unknown Springfield account"
quiet_niles contact add "Blue-Haired Lawyer"
quiet_niles contact add "Unidentified courthouse lead" --tag prospect

quiet_niles material add "Hutz Law engagement letter" \
  --url "https://example.invalid/hutz-law/engagement-letter" \
  --description "Standard fictional engagement letter" --tag sales --tag contract
quiet_niles material add "Monorail investigation outline" \
  --url "https://example.invalid/hutz-law/monorail-review" \
  --description "Proposed fictional investigation scope" --tag sales --tag pilot

quiet_niles report status --html crm-operating-report.html

echo
echo "Populated example CRM: $PWD"
echo "Operating report: $PWD/crm-operating-report.html"
echo "Open it in a browser to exercise search, stage filters, sorting, and history controls."
