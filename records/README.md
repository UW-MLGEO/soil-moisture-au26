# Published records (edit rarely, by hand)

Evidence that accompanies the published corpus. Their SHA-256 values appear in Kopah object keys and in
`ai_ready_dataset.json`, so editing one creates a new published record. Paths written *inside* these files
(e.g. `config/ismn_conus_sm_temperature.json`) are where the files were at publication; that specification is
now `locks/ismn.json` (byte-identical, same SHA-256).

| File | Contents |
| --- | --- |
| `ai_ready_dataset.json` | Catalog of the published components (v1 daily tables, Morgan labels): manifest URIs, counts, bytes |
| `source_provenance.json` | Per-source provider, version, retrieval, citation and rights evidence |
| `ismn_network_provenance.json`, `ismn_network_references.bib` | Citations and acknowledgements for every contributing ISMN network |
| `spatial_partitions.json` | Proposed station-ID spatial folds and southern holdout (not applied to the tables) |
| `legacy/daily_homogenization_policy.json` | QC/decoding policy that produced the v1 `daily/` tables (now `pipeline.yml`) |
