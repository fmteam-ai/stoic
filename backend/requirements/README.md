# Layered production environments (iter-104, safety review)

The root `requirements.txt` remains the FULL pinned lockfile used by the
preview environment, CI and the current monolithic Docker image — nothing
changes there. These layer manifests define the SPLIT production planes; each
lists only the top-level packages that plane imports, pinned via the master
lockfile with `-c ../requirements.txt`.

Install a plane:  `pip install -c ../requirements.txt -r api.txt`

| plane           | file            | contents                               |
|-----------------|-----------------|----------------------------------------|
| API / control   | api.txt         | FastAPI service, auth, payments, ops   |
| trading workers | workers.txt     | api + numeric stack + crypto bridge    |
| research / ML   | research.txt    | workers + torch, xgboost, lightgbm, sklearn |
| LLM services    | llm.txt         | api + emergentintegrations             |
| maintenance     | maintenance.txt | migrations & tooling                   |

Unused heavy dependencies found in the lockfile (imported nowhere in
`backend/`): pandas, matplotlib, plotly, litellm, openai (direct SDK),
boto3, playwright, stripe (payments flow goes through emergentintegrations).
They are candidates for removal from the lockfile in a dedicated cleanup —
kept for now to avoid breaking transitive pins.
