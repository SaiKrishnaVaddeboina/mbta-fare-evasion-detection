# Security

This repository is the archive of a completed academic capstone (WPI MS FinTech, Spring 2026). It is not actively maintained and is not deployed in production.

## Reporting issues

If you have spotted something that looks like a security or privacy concern — particularly anything that suggests rider-level data or `account_id`s have leaked into the repository — please contact the team via the WPI MS FinTech programme rather than opening a public GitHub issue.

## Data-handling commitments

- The MBTA / Masabi dataset is governed by a data-use agreement and is **not** redistributed in this repository. See [DATA.md](DATA.md) for the schema and access procedure.
- All rider-level outputs (feature tables, symbol streams, posterior scores, shortlists) are excluded from version control via [`.gitignore`](.gitignore). Only model-level artefacts that contain no rider identifiers are committed.
- The synthetic-data generator at [`scripts/generate_synthetic_data.py`](scripts/generate_synthetic_data.py) produces fully fabricated CSVs in the production schema and contains no real rider data.
