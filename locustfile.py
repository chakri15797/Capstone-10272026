import os
import re

from locust import HttpUser, between, task


PREDICTION_PAYLOAD = {
    "features": {
        "encounter_id": 123,
        "patient_nbr": 987,
        "race": "Caucasian",
        "gender": "Female",
        "age": "[50-60)",
        "weight": "[150-175)",
        "admission_type_id": 1,
        "discharge_disposition_id": 1,
        "admission_source_id": 7,
        "time_in_hospital": 4,
        "payer_code": "MC",
        "medical_specialty": "Cardiology",
        "num_lab_procedures": 12,
        "num_procedures": 1,
        "num_medications": 20,
        "number_outpatient": 0,
        "number_emergency": 0,
        "number_inpatient": 0,
        "diag_1": "250.83",
        "diag_2": "276.2",
        "diag_3": "401.9",
        "number_diagnoses": 5,
        "max_glu_serum": "None",
        "A1Cresult": "None",
        "metformin": "No",
        "repaglinide": "No",
        "nateglinide": "No",
        "chlorpropamide": "No",
        "glimepiride": "No",
        "acetohexamide": "No",
        "glipizide": "No",
        "glyburide": "No",
        "tolbutamide": "No",
        "pioglitazone": "No",
        "rosiglitazone": "No",
        "acarbose": "No",
        "miglitol": "No",
        "troglitazone": "No",
        "tolazamide": "No",
        "examide": "No",
        "citoglipton": "No",
        "insulin": "No",
        "glyburide-metformin": "No",
        "glipizide-metformin": "No",
        "glimepiride-pioglitazone": "No",
        "metformin-rosiglitazone": "No",
        "metformin-pioglitazone": "No",
        "change": "No",
        "diabetesMed": "Yes",
    }
}


class ReadmissionUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        username = os.getenv("LOCUST_USERNAME")
        password = os.getenv("LOCUST_PASSWORD")
        if not username or not password:
            raise RuntimeError("Set LOCUST_USERNAME and LOCUST_PASSWORD for authenticated load tests.")

        login_page = self.client.get("/login", name="/login")
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
        if login_page.status_code != 200 or csrf_match is None:
            raise RuntimeError("Could not retrieve the login CSRF token.")

        login_response = self.client.post(
            "/login",
            data={
                "csrf_token": csrf_match.group(1),
                "username": username,
                "password": password,
            },
            name="/login",
            allow_redirects=False,
        )
        if login_response.status_code != 303:
            raise RuntimeError(f"Load-test login failed with HTTP {login_response.status_code}.")

        csrf_response = self.client.get("/auth/csrf", name="/auth/csrf")
        if csrf_response.status_code != 200:
            raise RuntimeError(f"Could not retrieve session CSRF token: HTTP {csrf_response.status_code}.")
        self.csrf_token = csrf_response.json()["csrf_token"]

    @task
    def predict_readmission(self):
        with self.client.post(
            "/predict",
            json=PREDICTION_PAYLOAD,
            headers={"X-CSRF-Token": self.csrf_token},
            name="/predict",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"Prediction returned HTTP {response.status_code}")
                return

            try:
                body = response.json()
                prediction = body["prediction"]
                probability = float(body["probability"])
            except (KeyError, TypeError, ValueError) as error:
                response.failure(f"Invalid prediction response: {error}")
                return

            if prediction not in (0, 1) or not 0 <= probability <= 1:
                response.failure("Prediction response contains invalid values")
