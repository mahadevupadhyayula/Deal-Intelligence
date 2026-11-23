# Lead Research eval datasets (Spec 007)

- `icp.json` — the ICP seeded into each throwaway eval tenant (same shape as the Spec 005 fixtures).
- `corpora/*.json` — pinned search results (`frozen` mode: real model, no network search).
- `adversarial/` — attack cases (each names a `control`; the attack must not raise the tier).
  `gating: false` marks a documented residual risk: measured and reported, never silently dropped.
- `judgement/` — clean pages with expected tier **ranges** and extraction checks.
- `calibration/labelled_companies.json` — YOUR labelled real companies (git-ignored; copy the
  `.template.json`, `label` is `fit` or `not_fit`). Optional `calibration/icp.json` overrides `icp.json`.
  Without the file, `python -m agents.evals run calibration` prints "not run" and exits 0.
- All hosts in datasets must be reserved (`.example`, `.invalid`, `.test`); the loader enforces it.
