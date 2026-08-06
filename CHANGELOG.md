# Changelog

All notable changes to PyBench are recorded here. PyBench and scworkbench use
the same `0.1.x` release line, but compatibility is determined by the pinned
PyBench commit and PyBench protocol schema digest in the scworkbench composite
release metadata.

## 0.1.0 - 2026-08-06

- Added bundle-native Python getters and payload writers.
- Added scVI, PeakVI, Palantir, and ingestion bridges.
- Added the capability-scoped PyBench protocol v2 staging contract.
- Added mutation receipts, independent R-side mutation auditing, rollback, and
  process-replacement coverage.
- Pinned the Python package build backend for reproducible wheel and source
  distribution construction.
