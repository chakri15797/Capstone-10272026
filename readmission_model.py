import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.request import urlretrieve

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

DATASET_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/00296/dataset_diabetes.zip"


def make_readmission_target(df: pd.DataFrame) -> pd.DataFrame:
    """Create a binary 30-day intervention label from the original readmitted classes."""
    result = df.copy()
    if "readmitted" not in result.columns:
        raise KeyError("The dataset must include a 'readmitted' column.")

    mapping = {"NO": 0, "<30": 1, ">30": 0}
    result["readmission_30d"] = result["readmitted"].map(mapping)
    return result


def ensure_dataset_available(data_dir: str = "data") -> Path:
    """Download and unpack the hospital diabetes dataset if it is not present locally."""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)

    csv_candidates = [
        root / "diabetic_data.csv",
        root / "dataset_diabetes" / "diabetic_data.csv",
        Path("dataset_diabetes") / "diabetic_data.csv",
    ]
    for candidate in csv_candidates:
        if candidate.exists():
            return candidate

    zip_path = root / "dataset_diabetes.zip"
    if not zip_path.exists():
        print(f"Downloading dataset from {DATASET_URL} ...")
        urlretrieve(DATASET_URL, zip_path)

    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(root)

    for candidate in csv_candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError("Could not locate the diabetic data CSV after extraction.")


def load_hospital_readmission_data(data_dir: str = "data") -> pd.DataFrame:
    """Load the diabetes readmission dataset and replace missing qualifiers with NaN."""
    csv_path = ensure_dataset_available(data_dir)
    df = pd.read_csv(csv_path)
    df = df.replace("?", np.nan)
    return df


def describe_dataset(df: pd.DataFrame) -> Dict[str, object]:
    """Return a concise summary for EDA and monitoring."""
    summary = {
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
        "target_distribution": df.get("readmitted", pd.Series(dtype="object")).value_counts(dropna=False).to_dict(),
        "missing_values": df.isna().sum().to_dict(),
    }
    return summary


def plot_eda(df: pd.DataFrame, output_dir: str = "artifacts") -> Dict[str, Path]:
    """Create core exploratory visualizations and return their paths."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plot_paths: Dict[str, Path] = {}

    target_counts = df["readmitted"].value_counts().sort_index()
    fig, ax = plt.subplots(figsize=(8, 5))
    target_counts.plot(kind="bar", ax=ax, color=["#1f77b4", "#ff7f0e", "#2ca02c"])
    ax.set_title("Readmission Distribution")
    ax.set_xlabel("Readmission category")
    ax.set_ylabel("Count")
    fig.tight_layout()
    path = out_dir / "readmission_distribution.png"
    fig.savefig(path)
    plt.close(fig)
    plot_paths["target_distribution"] = path

    age_order = ["[0-10)", "[10-20)", "[20-30)", "[30-40)", "[40-50)", "[50-60)", "[60-70)", "[70-80)", "[80-90)", "[90-100)"]
    fig, ax = plt.subplots(figsize=(10, 5))
    sns.countplot(data=df, x="age", hue="readmitted", order=age_order, ax=ax)
    ax.set_title("Age Distribution by Readmission Category")
    ax.set_xlabel("Age band")
    ax.set_ylabel("Count")
    fig.tight_layout()
    path = out_dir / "age_readmission.png"
    fig.savefig(path)
    plt.close(fig)
    plot_paths["age_readmission"] = path

    fig, ax = plt.subplots(figsize=(8, 5))
    df["time_in_hospital"].hist(bins=20, ax=ax, edgecolor="black")
    ax.set_title("Hospital Stay Length Distribution")
    ax.set_xlabel("Time in hospital (days)")
    ax.set_ylabel("Count")
    fig.tight_layout()
    path = out_dir / "length_of_stay_distribution.png"
    fig.savefig(path)
    plt.close(fig)
    plot_paths["length_of_stay"] = path

    return plot_paths


def _build_preprocessor(X: pd.DataFrame) -> ColumnTransformer:
    numeric_features = X.select_dtypes(include=[np.number]).columns.tolist()
    categorical_features = [col for col in X.columns if col not in numeric_features]

    transformers = []
    if numeric_features:
        transformers.append(
            (
                "numeric",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="median")),
                        ("scaler", StandardScaler()),
                    ]
                ),
                numeric_features,
            )
        )
    if categorical_features:
        transformers.append(
            (
                "categorical",
                Pipeline(
                    steps=[
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("encoder", OneHotEncoder(handle_unknown="ignore", sparse_output=False)),
                    ]
                ),
                categorical_features,
            )
        )

    return ColumnTransformer(transformers=transformers, remainder="drop")


def _classification_metrics(y_true: pd.Series, y_pred: np.ndarray, y_prob: np.ndarray) -> Dict[str, float]:
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_true, y_prob)),
    }


def prepare_model_data(df: pd.DataFrame, target_col: str = "readmitted") -> Tuple[pd.DataFrame, pd.Series]:
    """Create the binary readmission target and select model features."""
    data = df.copy()
    data = data.replace("?", np.nan)

    if "readmission_30d" not in data.columns and target_col == "readmitted":
        data = make_readmission_target(data)
        target_col = "readmission_30d"

    if target_col not in data.columns:
        raise ValueError(f"Target column '{target_col}' not found in the dataset.")

    excluded = {"encounter_id", "patient_nbr", "readmitted", "readmission_30d"}
    feature_columns = [col for col in data.columns if col not in excluded]
    X = data[feature_columns].copy()
    y = data[target_col].astype(int)
    return X, y


def _candidate_models() -> List[Tuple[str, object]]:
    return [
        (
            "logistic_regression",
            LogisticRegression(
                max_iter=2000,
                class_weight="balanced",
                solver="liblinear",
                random_state=42,
            ),
        ),
        (
            "random_forest",
            RandomForestClassifier(
                n_estimators=100,
                max_depth=None,
                min_samples_leaf=5,
                class_weight="balanced",
                random_state=42,
                n_jobs=2,
            ),
        ),
        (
            "gradient_boosting",
            GradientBoostingClassifier(random_state=42),
        ),
    ]


def train_and_select_models(
    df: pd.DataFrame,
    target_col: str = "readmitted",
    random_state: int = 42,
) -> Dict[str, object]:
    """Train and compare multiple classifiers, choose the best on validation AUC, and report final test metrics."""
    X, y = prepare_model_data(df, target_col=target_col)

    X_train, X_temp, y_train, y_temp = train_test_split(
        X,
        y,
        test_size=0.30,
        random_state=random_state,
        stratify=y,
    )
    X_val, X_test, y_val, y_test = train_test_split(
        X_temp,
        y_temp,
        test_size=0.5,
        random_state=random_state,
        stratify=y_temp,
    )

    models: Dict[str, Dict[str, object]] = {}
    for model_name, estimator in _candidate_models():
        fit_start = time.perf_counter()
        print(f"Training {model_name}...", flush=True)
        model_pipeline = Pipeline(
            steps=[
                ("preprocessor", _build_preprocessor(X_train)),
                ("model", estimator),
            ]
        )
        model_pipeline.fit(X_train, y_train)
        print(
            f"Finished {model_name} training in {time.perf_counter() - fit_start:.1f}s.",
            flush=True,
        )

        train_prob = model_pipeline.predict_proba(X_train)[:, 1]
        val_prob = model_pipeline.predict_proba(X_val)[:, 1]
        test_prob = model_pipeline.predict_proba(X_test)[:, 1]
        train_pred = model_pipeline.predict(X_train)
        val_pred = model_pipeline.predict(X_val)
        test_pred = model_pipeline.predict(X_test)

        model_metrics = {
            "train_metrics": _classification_metrics(y_train, train_pred, train_prob),
            "validation_metrics": _classification_metrics(y_val, val_pred, val_prob),
            "test_metrics": _classification_metrics(
                y_test,
                test_pred,
                test_prob,
            ),
        }
        models[model_name] = {
            "pipeline": model_pipeline,
            "train_metrics": model_metrics["train_metrics"],
            "validation_metrics": model_metrics["validation_metrics"],
            "test_metrics": model_metrics["test_metrics"],
        }

    best_model_name = max(
        models,
        key=lambda name: (
            models[name]["validation_metrics"]["roc_auc"],
            models[name]["validation_metrics"]["f1"],
        ),
    )
    best_pipeline = models[best_model_name]["pipeline"]

    train_val_X = pd.concat([X_train, X_val], ignore_index=True)
    train_val_y = pd.concat([y_train, y_val], ignore_index=True)
    retrained_pipeline = clone(best_pipeline)
    refit_start = time.perf_counter()
    print(f"Retraining selected model: {best_model_name}...", flush=True)
    retrained_pipeline.fit(train_val_X, train_val_y)
    print(
        f"Finished final {best_model_name} training in "
        f"{time.perf_counter() - refit_start:.1f}s.",
        flush=True,
    )

    test_prob = retrained_pipeline.predict_proba(X_test)[:, 1]
    test_pred = retrained_pipeline.predict(X_test)
    final_metrics = _classification_metrics(y_test, test_pred, test_prob)
    final_metrics["validation_auc"] = float(models[best_model_name]["validation_metrics"]["roc_auc"])

    summary = {
        "models": {
            name: {
                "train_metrics": metrics["train_metrics"],
                "validation_metrics": metrics["validation_metrics"],
                "test_metrics": metrics["test_metrics"],
            }
            for name, metrics in models.items()
        },
        "best_model_name": best_model_name,
        "best_model": retrained_pipeline,
        "test_metrics": final_metrics,
        "train_metrics": models[best_model_name]["train_metrics"],
        "validation_auc": float(models[best_model_name]["validation_metrics"]["roc_auc"]),
        "test_confusion_matrix": confusion_matrix(y_test, test_pred).tolist(),
        "test_classification_report": classification_report(y_test, test_pred, target_names=["No readmission", "Readmitted"], output_dict=True),
    }
    return summary


def save_model_artifact(model, output_path: str = "artifacts/final_readmission_model.joblib") -> str:
    """Persist the trained model pipeline to disk for downstream API deployment."""
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output_file)
    print(f"Saved trained model to: {output_file}")
    return str(output_file)


def save_evaluation_report(summary: Dict[str, object], output_path: str = "artifacts/model_evaluation_report.json") -> str:
    """Save the key metrics for reproducible evaluation and auditing."""
    report_path = Path(output_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    serializable = {
        "best_model_name": summary["best_model_name"],
        "validation_auc": float(summary["validation_auc"]),
        "test_metrics": {k: float(v) if isinstance(v, (int, float, np.floating)) else v for k, v in summary["test_metrics"].items()},
        "models": {
            model_name: {
                metric_name: {
                    key: float(value) if isinstance(value, (int, float, np.floating)) else value
                    for key, value in metrics.items()
                }
                for metric_name, metrics in model_metrics.items()
            }
            for model_name, model_metrics in summary["models"].items()
        },
    }

    with report_path.open("w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2)

    print(f"Saved evaluation report to: {report_path}")
    return str(report_path)


def run_training_pipeline(data_dir: str = "data") -> Dict[str, object]:
    """Execute the end-to-end training workflow and return the selected model summary."""
    df = load_hospital_readmission_data(data_dir)
    eda_summary = describe_dataset(df)
    plot_eda(df)

    summary = train_and_select_models(df, target_col="readmitted")
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    model_artifact = f"artifacts/readmission_model_{run_id}.joblib"
    report_path = f"artifacts/readmission_model_{run_id}_evaluation.json"
    save_model_artifact(summary["best_model"], model_artifact)
    save_evaluation_report(summary, report_path)

    output = {
        "eda_summary": eda_summary,
        "selected_model": summary["best_model_name"],
        "validation_auc": summary["validation_auc"],
        "test_metrics": summary["test_metrics"],
        "model_artifact": model_artifact,
        "report_path": report_path,
    }
    print("EDA summary:")
    print(eda_summary)
    print("\nBest model selected:", summary["best_model_name"])
    print("Validation ROC AUC:", round(summary["validation_auc"], 4))
    print("Test metrics:", summary["test_metrics"])
    return output


if __name__ == "__main__":
    run_training_pipeline()
