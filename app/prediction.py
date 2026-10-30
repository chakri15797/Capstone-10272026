from functools import lru_cache
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from app.schemas import Features

MODEL_PATH = Path(__file__).resolve().parent.parent / "artifacts" / "final_readmission_model.joblib"


@lru_cache(maxsize=1)
def _load_model() -> Any:
    return joblib.load(MODEL_PATH)


def _prediction_features(features: Features) -> pd.DataFrame:
    frame = pd.DataFrame([features.model_dump(by_alias=True)])
    return frame.replace({"None": None, "nan": None, "": None})


def predict_features(features: Features) -> tuple[int, float]:
    model = _load_model()
    frame = _prediction_features(features)
    prediction = int(model.predict(frame)[0])
    probability = float(model.predict_proba(frame)[0, 1])
    return prediction, probability
