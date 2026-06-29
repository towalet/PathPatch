"""
Pydantic schema for the AI diagnosis report (docs/AGENT_PLAN.md §12-§13).

The model is the validation gate between raw model output and the database. It
enforces structure, types, ranges, and the "no certainty" rule; the remaining
guardrails that need context the schema can't see — clamping to
``PATCHPATH_MAX_CONFIDENCE`` and rejecting evidence that cites un-uploaded files
— live in :mod:`report_generator`.

Expected model JSON (keys exactly): root_cause, confidence_score, severity,
detected_stack, detected_cloud_provider, explanation, evidence[], recommended_fix,
commands_to_run[], verification_checklist[], missing_information[], possible_risks[].
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# A report claiming at least this much confidence MUST cite evidence; below it the
# report is "explicitly low confidence" and may stand on missing information alone.
LOW_CONFIDENCE_CEILING = 0.35


class EvidenceItem(BaseModel):
    """One citation backing the diagnosis. ``source`` must be an uploaded file."""

    model_config = ConfigDict(extra="ignore")

    source: str = Field(min_length=1)
    line_or_section: str = ""
    reason: str = Field(min_length=1)


class DiagnosisReportModel(BaseModel):
    """Schema-validated diagnosis. Unknown keys are ignored so a chatty model
    doesn't fail an otherwise-valid response."""

    model_config = ConfigDict(extra="ignore")

    root_cause: str = Field(min_length=1)
    # Allow 1.0 through the schema; the generator clamps it below 1.0 rather than
    # burning a retry on an otherwise-good report.
    confidence_score: float = Field(ge=0, le=1)
    severity: Literal["low", "medium", "high"]
    detected_stack: list[str] = Field(default_factory=list)
    detected_cloud_provider: str = ""
    explanation: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    recommended_fix: str = Field(min_length=1)
    commands_to_run: list[str] = Field(default_factory=list)
    verification_checklist: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    possible_risks: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_required_unless_low_confidence(self) -> DiagnosisReportModel:
        if self.confidence_score >= LOW_CONFIDENCE_CEILING and not self.evidence:
            raise ValueError(
                "evidence must be non-empty unless confidence_score is below "
                f"{LOW_CONFIDENCE_CEILING}"
            )
        return self

    def to_orm_fields(self) -> dict:
        """Project the validated model onto ``DiagnosisReport`` column names."""
        return {
            "root_cause": self.root_cause,
            "confidence_score": self.confidence_score,
            "severity": self.severity,
            "detected_stack": self.detected_stack,
            "detected_cloud_provider": self.detected_cloud_provider,
            "explanation": self.explanation,
            "evidence_json": [item.model_dump() for item in self.evidence],
            "recommended_fix": self.recommended_fix,
            "commands_json": self.commands_to_run,
            "verification_checklist_json": self.verification_checklist,
            "missing_information_json": self.missing_information,
            "possible_risks_json": self.possible_risks,
        }
