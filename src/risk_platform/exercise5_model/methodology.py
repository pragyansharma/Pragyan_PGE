"""Scoring function used by the production runner.

The data-science notebook was not in the assessment packet, so this body is a
stand-in with the interface the runner calls: a feature frame and a parameter
file in, scores out. The body is replaced when their function arrives.

Null features raise here. The runner does not impute them.
"""

from __future__ import annotations

import pandas as pd


def score_frame(features: pd.DataFrame, params: dict) -> pd.DataFrame:
    required = ["entity_id", "proximity_ft", "encroachment_present"]
    missing = [column for column in required if column not in features.columns]
    if missing:
        raise ValueError(
            "score_frame expected columns " + ", ".join(required) + ". Missing: " + ", ".join(missing)
        )
    if features["proximity_ft"].isna().any() or features["encroachment_present"].isna().any():
        raise ValueError("score_frame does not impute null features.")

    weights = params["weights"]
    scale = float(params["proximity_scale_ft"])
    proximity = features["proximity_ft"].astype("float64")
    encroachment = features["encroachment_present"].astype("float64")
    score = weights["proximity"] * (1.0 / (1.0 + proximity / scale)) + weights["encroachment"] * encroachment

    high = float(params["category_thresholds"]["high"])
    medium = float(params["category_thresholds"]["medium"])
    category = pd.Series("low", index=features.index)
    category = category.mask(score >= medium, "medium")
    category = category.mask(score >= high, "high")

    return pd.DataFrame(
        {
            "entity_id": features["entity_id"].astype("string"),
            "risk_score": score.astype("float64"),
            "risk_category": category.astype("string"),
        }
    )
