# Hutz Law populated CRM

This example builds a realistic CRM entirely through public `niles` commands
and generates the operating report. It is deliberately richer than a unit
fixture: it includes contracting and pilot deals, a stalled dependency, a warm
introduction, lost accounts, account-to-person mappings, dated interactions,
owned tasks, commercial values, materials, and intentional cleanup warnings.
Every organization, person, interaction, value, and URL in the fixture is
fictional and belongs to the Lionel Hutz/Springfield running example.

From the Niles repository:

```bash
./examples/hutz-law-crm/populate.sh /tmp/hutz-law-demo
```

Then open:

```text
/tmp/hutz-law-demo/crm-operating-report.html
```

The script refuses to overwrite an existing Niles project. Choose a new target
directory for each run. Set `NILES_PYTHON` when a specific Python interpreter
should run the installed package:

```bash
NILES_PYTHON=.venv/bin/python ./examples/hutz-law-crm/populate.sh /tmp/hutz-law-demo-venv
```
