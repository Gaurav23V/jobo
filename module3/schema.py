"""Pydantic models for Gemini structured outputs."""

from pydantic import BaseModel, Field


class FitDecisionOutput(BaseModel):
    fit_score: int = Field(
        ge=0,
        le=5,
        description="Integer 0-5: how strong a fit this job is for the candidate.",
    )
    reasoning: str = Field(
        default="",
        description="Unstructured explanation: good fit, bad fit, gaps, preferences.",
    )
    highlighted_project_names: list[str] = Field(
        default_factory=list,
        description=(
            "Project names that exactly match ## headings in the candidate profile "
            "markdown; used to tailor resume emphasis."
        ),
    )


class MaterialsOutput(BaseModel):
    resume_needs_update: bool = Field(
        description="True if resume LaTeX should be replaced with resume_latex.",
    )
    resume_latex: str = Field(
        default="",
        description="Full LaTeX document when tailoring; empty if base resume is fine.",
    )
    cover_letter: str = Field(
        default="",
        description="Concise personalized cover letter (plain text or markdown).",
    )
