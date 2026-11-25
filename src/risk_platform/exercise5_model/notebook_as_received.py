"""The notebook as it is run by hand today.

Paths are hard-coded and there is no freshness check. This module is not scheduled.
The production runner calls the same score_frame with the paths passed in.
"""

from pathlib import Path

import pandas as pd
import yaml

from risk_platform.exercise5_model.methodology import score_frame

# Paths as they stand in the notebook. The scheduled job receives its paths as arguments.
FEATURES_PATH = r"C:\Users\analyst\Desktop\features.csv"
PARAMS_PATH = r"C:\Users\analyst\Desktop\params.yaml"


def run_notebook() -> pd.DataFrame:
    features = pd.read_csv(FEATURES_PATH)
    params = yaml.safe_load(Path(PARAMS_PATH).read_text(encoding="utf-8"))
    return score_frame(features, params)
