#!/bin/bash
# Setup script for running on a remote server
set -e

echo "=== PhosphoAtlas Agent Benchmark Setup ==="

# Create venv
python3 -m venv .venv
source .venv/bin/activate

# Install deps
pip install -r requirements.txt

# Verify databases exist
echo ""
echo "Checking database files..."
for f in databases/psp/Kinase_Substrate_Dataset databases/signor/signor_phospho_human.json databases/uniprot/uniprot_phospho_parsed.json; do
    if [ -f "$f" ]; then
        echo "  OK: $f"
    else
        echo "  MISSING: $f"
    fi
done

# Verify gold standard
echo ""
echo "Checking gold standard..."
if ls gold_standard/input/*.xlsx 1>/dev/null 2>&1; then
    echo "  OK: PA2 XLSX found in gold_standard/input/"
else
    echo "  MISSING: Place PA2 XLSX in gold_standard/input/"
fi

# Quick self-test
echo ""
echo "Running database self-test..."
python -c "
from databases.tools import DatabaseTools
tools = DatabaseTools('databases/')
dbs = tools.list_databases()
for db in dbs['databases']:
    stats = tools.get_stats(db['id'])
    print(f\"  {db['id']}: {stats.get('total_entries',0)} entries, {stats.get('unique_kinases',0)} kinases\")
print('Self-test PASSED')
"

echo ""
echo "Setup complete. Next steps:"
echo "  1. Place PA2 XLSX in gold_standard/input/"
echo "  2. python run_experiment.py --step parse --pa2-input gold_standard/input/YOUR_FILE.xlsx"
echo "  3. Implement your agent runner in agents/"
echo "  4. python run_experiment.py --step score --atlas results/raw/YOUR_ATLAS.json"
