# datasets/

Git-ignored. Holds dataset versions, each in its own folder with a `manifest.json`
(provenance, licence, synthetic flag, per-file hashes) and immutable `raw/` copies of every
imported source file.

- `nseql fixtures` creates `synthetic_v1/` — generated data, not market data.
- `nseql import-bhavcopy <id> <files…> --licence "…"` imports NSE bhavcopy files that **you**
  are entitled to use. The licence statement is mandatory and is shown in every report.

Never commit anything in this folder.
