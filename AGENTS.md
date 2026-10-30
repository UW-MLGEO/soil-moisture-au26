# Repository Guidelines

## Project

Results must be reproducible from a fresh clone using documented commands. Produce only requested deliverables and clarify unresolved decisions before implementing dependent work.

## Environment and compute

- Run commands from the repository root using the configured environment and package manager. Declare dependencies in its manifest.
- Update the lockfile with dependency changes and document any platform or hardware requirements.

## Data and provenance

- Fetch data through reproducible scripts; record source URLs or DOIs, versions, and retrieval dates.
- Preserve raw data; write processed data separately. For cleaning, document each replacement or removal rule and report affected columns and counts.
- Keep generated outputs, caches, and downloaded data in ignored directories; track required reference metadata.
- Downloads must accept a cache directory, validate cached files, and fetch only missing or invalid files.

## Code and workflow

- Use repository-relative paths or configurable paths; do not hard-code machine-specific locations.
- Document each script's purpose, runnable invocation, and prerequisites.
- Document commands that regenerate reported results. Changed notebooks must run top to bottom from a fresh kernel.

## Reporting and credit

- State units, reference frames, time windows, and uncertainty assumptions where relevant; document formats and alignment at data boundaries.
- The user makes commits unless explicitly requested otherwise. Requested commit messages disclose AI assistance: `AI-assisted: <tools>, <what they drafted>`.

## Verification

- After code or environment changes, run the documented smoke test and checks covering affected behavior. Use small, deterministic inputs independent of large downloads and private data.
- Describe the behavior or edge case each test verifies, including relevant missing inputs and boundary conditions.
- Report exact verification commands and outcomes, including the smoke test's last line when run.
- State failed or skipped checks and unavailable data explicitly; do not claim unverified results.
