# Audit Report 2026-04-15

## Scope

Release audit for recommendation publication lineage, LLM outcome backlog behavior and historical release documentation integrity.

## Findings

- LLM outcome SQL prefilter could skip newer matured rows when a backlog contained many older legacy rows.
- README/CHANGELOG referenced historical audit artifacts that were absent from the archive.
- Release documentation needed a regression guard to prevent broken references.

## Fixes

- Added regression coverage for matured-row outcome processing under legacy backlog.
- Restored historical audit placeholders and release-integrity tests.
- Documented residual operational and financial risks explicitly.

## Риски

- Proxy outcomes are not exchange truth and can differ materially from real realized PnL.
- Operator decisions, manual sizing and external execution quality remain outside the recommender's control.
- Staging validation is mandatory before using the system with real capital.
