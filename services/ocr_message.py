"""Shared OCR input contract for MQTT and HTTP."""

from pydantic import BaseModel, ConfigDict, Field


class OcrMessage(BaseModel):
    # Keep unknown producer metadata compatible, but do not persist it.
    model_config = ConfigDict(strict=True, str_strip_whitespace=True, extra="ignore")

    text: str = Field(min_length=1)
    task_id: str = Field(default="default", min_length=1)
