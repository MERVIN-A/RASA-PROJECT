# 🌱 Rasa Eco-Travel Advisor

A Rasa Open Source (NLU + Core) chatbot that helps travellers plan lower-carbon trips. It collects trip details through an adaptive form, calculates carbon footprints per transport mode (Climatiq), fetches flights and hotels (Amadeus sandbox), ranks options with a weighted carbon/price/preference score, shows colour-coded results in a ReactJS frontend, and hands complex cases to a human advisor with full context.

## Repository structure

```
config.yml               DIETClassifier pipeline (primary) + policies
config_distilbert.yml    optional DistilBERT pipeline (LanguageModelFeaturizer)
domain.yml               intents, entities, slots, trip_form, responses, actions
data/nlu.yml             NLU training data (18 intents, 7 entities)
data/rules.yml           rules incl. fallback, form interruptions, handover consent
data/stories.yml         multi-turn stories
actions/actions.py       custom actions (Climatiq, Amadeus, ranking, handover, fallback)
actions/scoring.py       pure scoring + carbon banding functions
actions/data/*.json      fallback geodata, curated eco-hotel and offset databases
frontend/index.html      ReactJS chat UI (Rasa REST channel), vendor/ = React UMD builds
tests/test_stories.yml   test stories for `rasa test core`
tests/test_*.py          pytest unit tests with mocked API responses
scripts/                 plot_metrics.py, measure_latency.py, screenshots.py
docs/                    figures and Graphviz sources used in the report
Dockerfile*, docker-compose.yml, .env.example
```

## 1. Run locally (Python 3.10)

```bash
git clone https://github.com/<your-username>/eco-travel-advisor.git
cd eco-travel-advisor
python3.10 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # add your API keys (optional: the bot works offline with fallbacks)
export $(grep -v '^#' .env | xargs)                        # Windows: set the variables manually

rasa train                                                  # ~3 min on a laptop CPU
rasa run actions --port 5055                                # terminal 1
rasa run --enable-api --cors "*" --port 5005                # terminal 2
python -m http.server 3000 --directory frontend             # terminal 3 -> http://localhost:3000
```

Without API keys every action falls back to static emission factors, price estimates and the curated JSON database, and tells the user the figures are estimates.

### API keys (all free tiers)
| Service | Used for | Variable |
|---|---|---|
| Climatiq | kg CO2e per transport mode | `CLIMATIQ_API_KEY` |
| Amadeus for Developers (sandbox) | flight offers, hotel list | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
| OpenCage | geocoding / GPS reverse geocoding | `OPENCAGE_API_KEY` |

Climatiq activity IDs are configurable (`CLIMATIQ_RAIL_ID`, etc.); check them in the Climatiq Data Explorer for the data version you use.

## 2. Testing

```bash
pytest -q tests/                                            # unit tests (mocked APIs)
rasa data split nlu --training-fraction 0.8 --random-seed 42
rasa train nlu --nlu train_test_split/training_data.yml --fixed-model-name nlu-split
rasa test nlu --nlu train_test_split/test_data.yml --model models/nlu-split.tar.gz --out results/split
rasa test nlu --nlu data/nlu.yml --cross-validation --folds 3 --out results/cv
rasa test core --stories tests/test_stories.yml --out results/core
python scripts/measure_latency.py                           # with the bot running
python scripts/plot_metrics.py                              # regenerates report figures
```

## 3. Docker

```bash
cp .env.example .env     # fill in keys, set RASA_TOKEN and ALLOWED_ORIGIN
docker compose up --build
# frontend http://localhost:3000   Rasa API http://localhost:5005   action server: internal network only
```

## 4. Cloud deployment (HuggingFace Spaces, recommended)
1. Create a new Space → SDK **Docker**.
2. Push this repository to the Space (or link the GitHub repo).
3. Add `CLIMATIQ_API_KEY`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `OPENCAGE_API_KEY`, `RASA_TOKEN` under *Settings → Variables and secrets* (encrypted, never in Git).
4. Spaces expose one port, so run Rasa + actions + nginx in one container (or host the frontend on GitHub Pages and point `?rasa=` to the Space URL).

AWS/Azure/GCP work with the same images (ECS, App Service, Cloud Run) but watch free-tier billing.

### Secure development tunnel (Pyngrok)
```python
from pyngrok import ngrok, conf
conf.get_default().auth_token = os.environ["NGROK_AUTHTOKEN"]
print(ngrok.connect(5005, "http").public_url)   # share only for testing, then close
```

## 5. Privacy and security
- In-memory tracker store: no conversation data is persisted by default; `delete my data` restarts the session.
- GPS only after browser consent; coordinates rounded and discarded after converting to a city.
- Handover only after explicit consent; payload written to the advisor webhook or `handover_queue/`.
- `.env` is git-ignored; secrets only via environment variables; Rasa API protected by token; CORS restricted.

## Licence
Educational project. Hotel and offset data in `actions/data` are demo data — verify before real use.

## 6. Results snapshot (docs/results)
| Version | Change | Held-out accuracy | Macro F1 |
|---|---|---|---|
| v1 | baseline data | 0.58 | 0.47 |
| v2 | data doubled and diversified (540 examples) | 0.78 | 0.77 |
| v3 | DIET epochs 100 → 200 | 0.77 | 0.77 |

`rasa test core`: 9/9 stories, 69/69 actions correct · pytest: 21/21 passed · max turn latency 0.20 s (offline fallbacks).

## 7. Uploading this project to GitHub
1. Create an empty repository named `eco-travel-advisor` on github.com (no README).
2. In this folder: `git init && git add . && git commit -m "Eco-Travel Advisor" && git branch -M main`
3. `git remote add origin https://github.com/<your-username>/eco-travel-advisor.git && git push -u origin main`
4. Check that `.env` is NOT in the repository, then paste the repository link into the report cover page.
