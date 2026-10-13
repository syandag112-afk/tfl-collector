Headway Desk TfL collector on GitHub Actions. See SETUP.md.
scripts/tfl_core.py = collection logic (reused from the old cloud collector); scripts/collector.py = local-disk storage + commands.
Local test: python3 scripts/collector.py collect   (run twice a few minutes apart, then: rollup YYYY-MM-DD)
