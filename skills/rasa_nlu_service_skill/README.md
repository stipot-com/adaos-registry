# Rasa NLU Service Skill

Local AdaOS service wrapper around the NLU-only `rasa-port` slice.

Endpoints:

- `GET /health`
- `POST /train` with `project_dir`, `out_dir`, optional `fixed_model_name`
- `POST /parse` with `text`, optional `model_path`

The service intentionally exposes only NLU training and parsing. It does not
load Rasa Core policies, channels, action servers, Duckling, or ConveRT.

The skill runs in its own service venv. `requirements.in` contains the light
NLU stack for the default AdaOS pipeline (`CRFEntityExtractor` +
`LogisticRegressionClassifier`) and intentionally omits TensorFlow.
