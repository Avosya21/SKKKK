from __future__ import annotations



from datetime import date, datetime



from pydantic import BaseModel





class PipelineStepOut(BaseModel):

    id: str

    label: str

    status: str

    detail: str | None = None





class ArtifactProgressOut(BaseModel):

    phase: str

    current_label: str

    steps: list[PipelineStepOut]





class ArtifactOut(BaseModel):

    id: int

    artifact_id: str

    original_filename: str

    file_type: str

    source_type: str

    status: str

    upload_date: datetime

    error_message: str | None = None

    duplicate: bool = False

    transcript_html_url: str | None = None

    download_available: bool = False

    download_filename: str | None = None

    progress: ArtifactProgressOut | None = None

    lab_runs: int = 0

    meeting_lab: bool = False

    speakers_match_available: bool = False

    speakers_match_ready: bool = False

    speakers_confirm_ready: bool = False

    # Дата материала (REQUIREMENTS-PLAN этап 1): None — известна только дата загрузки; source — meeting/filename/media/manual/upload.

    material_date: date | None = None

    material_date_source: str = "upload"

    # переписка: день последнего сообщения (период «с … по …», SOURCE-ANCHORS-PLAN этап 1)

    material_period_end: date | None = None



    model_config = {"from_attributes": True}

