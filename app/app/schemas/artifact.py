from __future__ import annotations



from datetime import datetime



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



    model_config = {"from_attributes": True}

