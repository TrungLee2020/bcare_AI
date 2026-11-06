"""
Schema structured output (strict) cho hai bước đọc ảnh.

Mọi model đều `extra="forbid"` và không có giá trị mặc định: strict mode của
OpenAI đòi mọi trường có trong `required` và `additionalProperties: false`.
Trường không có trên giấy thì model trả null, không bỏ trường.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1"

DocType = Literal[
    "prescription",
    "lab_result",
    "medical_record",
    "imaging_report",
    "imaging_film",
    "other_medical",
    "not_medical",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Classification(_Strict):
    doc_type: DocType
    readable: bool
    confidence: float


class _Common(_Strict):
    ocr_text: str
    doc_date: str | None
    facility: str | None
    doctor: str | None
    patient_name: str | None
    patient_birth_year: int | None
    patient_gender: str | None
    uncertain_fields: list[str]


class Medication(_Strict):
    name: str
    strength: str | None
    form: str | None
    quantity: str | None
    instructions_as_written: str | None
    duration: str | None


class PrescriptionExtraction(_Common):
    diagnosis_as_written: str | None
    medications: list[Medication]
    follow_up_date: str | None
    notes: str | None


class LabTest(_Strict):
    name: str
    value: str | None
    unit: str | None
    reference_range: str | None
    flag_as_printed: str | None


class LabResultExtraction(_Common):
    tests: list[LabTest]


class MedicalRecordExtraction(_Common):
    record_kind: Literal["phieu_kham", "giay_ra_vien", "tom_tat_benh_an", "khac"]
    diagnosis_as_written: str | None
    treatment_as_written: str | None
    follow_up_date: str | None


class ImagingReportExtraction(_Common):
    modality: Literal["x_quang", "sieu_am", "ct", "mri", "noi_soi", "khac"]
    body_part: str | None
    findings_as_written: str | None
    conclusion_as_written: str | None


class OtherExtraction(_Common):
    pass


EXTRACTION_SCHEMAS: dict[str, type[BaseModel]] = {
    "prescription": PrescriptionExtraction,
    "lab_result": LabResultExtraction,
    "medical_record": MedicalRecordExtraction,
    "imaging_report": ImagingReportExtraction,
    "other_medical": OtherExtraction,
}
