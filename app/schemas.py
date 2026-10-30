from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Features(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    encounter_id: Optional[int] = Field(None, ge=0, description="Unique identifier for the encounter")
    patient_nbr: Optional[int] = Field(None, ge=0, description="Unique identifier for the patient")
    race: str = Field(min_length=1, description="Patient's race")
    gender: str = Field(min_length=1, description="Patient's gender")
    age: str = Field(min_length=1, description="Patient's age group")
    weight: Optional[str] = Field(None, description="Patient's weight range")
    admission_type_id: int = Field(ge=0, description="Type of admission")
    discharge_disposition_id: int = Field(ge=0, description="Discharge status")
    admission_source_id: int = Field(ge=0, description="Source of admission")
    time_in_hospital: int = Field(ge=0, description="Number of days spent in the hospital")
    payer_code: Optional[str] = Field(None, description="Insurance payer code")
    medical_specialty: Optional[str] = Field(None, description="Specialty of the admitting physician")
    num_lab_procedures: Optional[int] = Field(None, ge=0, description="Number of lab procedures performed")
    num_procedures: Optional[int] = Field(None, ge=0, description="Number of procedures performed")
    num_medications: Optional[int] = Field(None, ge=0, description="Number of medications prescribed")
    number_outpatient: Optional[int] = Field(None, ge=0, description="Number of outpatient visits")
    number_emergency: Optional[int] = Field(None, ge=0, description="Number of emergency visits")
    number_inpatient: Optional[int] = Field(None, ge=0, description="Number of inpatient visits")
    diag_1: Optional[str] = Field(None, description="Primary diagnosis (ICD code)")
    diag_2: Optional[str] = Field(None, description="Secondary diagnosis (ICD code)")
    diag_3: Optional[str] = Field(None, description="Tertiary diagnosis (ICD code)")
    number_diagnoses: Optional[int] = Field(None, ge=0, description="Total number of diagnoses")
    max_glu_serum: Optional[str] = Field(None, description="Maximum glucose serum test result")
    A1Cresult: Optional[str] = Field(None, description="Hemoglobin A1c test result")
    metformin: Optional[str] = Field(None, description="Metformin medication status")
    repaglinide: Optional[str] = Field(None, description="Repaglinide medication status")
    nateglinide: Optional[str] = Field(None, description="Nateglinide medication status")
    chlorpropamide: Optional[str] = Field(None, description="Chlorpropamide medication status")
    glimepiride: Optional[str] = Field(None, description="Glimepiride medication status")
    acetohexamide: Optional[str] = Field(None, description="Acetohexamide medication status")
    glipizide: Optional[str] = Field(None, description="Glipizide medication status")
    glyburide: Optional[str] = Field(None, description="Glyburide medication status")
    tolbutamide: Optional[str] = Field(None, description="Tolbutamide medication status")
    pioglitazone: Optional[str] = Field(None, description="Pioglitazone medication status")
    rosiglitazone: Optional[str] = Field(None, description="Rosiglitazone medication status")
    acarbose: Optional[str] = Field(None, description="Acarbose medication status")
    miglitol: Optional[str] = Field(None, description="Miglitol medication status")
    troglitazone: Optional[str] = Field(None, description="Troglitazone medication status")
    tolazamide: Optional[str] = Field(None, description="Tolazamide medication status")
    examide: Optional[str] = Field(None, description="Examide medication status")
    citoglipton: Optional[str] = Field(None, description="Citoglipton medication status")
    insulin: Optional[str] = Field(None, description="Insulin medication status")
    glyburide_metformin: Optional[str] = Field(
        None, alias="glyburide-metformin", description="Glyburide-metformin medication status"
    )
    glipizide_metformin: Optional[str] = Field(
        None, alias="glipizide-metformin", description="Glipizide-metformin medication status"
    )
    glimepiride_pioglitazone: Optional[str] = Field(
        None, alias="glimepiride-pioglitazone", description="Glimepiride-pioglitazone medication status"
    )
    metformin_rosiglitazone: Optional[str] = Field(
        None, alias="metformin-rosiglitazone", description="Metformin-rosiglitazone medication status"
    )
    metformin_pioglitazone: Optional[str] = Field(
        None, alias="metformin-pioglitazone", description="Metformin-pioglitazone medication status"
    )
    change: Optional[str] = Field(None, description="Change in diabetes medication")
    diabetesMed: Optional[str] = Field(None, description="Whether the patient is on diabetes medication")


class PredictionRequest(BaseModel):
    features: Features


class PredictionResponse(BaseModel):
    prediction: int
    probability: float
    model_name: str
    status: str = "ok"
