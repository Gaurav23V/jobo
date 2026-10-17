"""Pydantic models for Module 3 structured outputs."""

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
        description="True if the Skills and/or Projects blocks should be replaced.",
    )
    skills_section: str = Field(
        default="",
        description=(
            "Full LaTeX of the \\section{Programming Skills} block when tailoring it; "
            "empty to keep the base block."
        ),
    )
    projects_section: str = Field(
        default="",
        description=(
            "Full LaTeX of the \\section{Projects} block when tailoring it; "
            "empty to keep the base block."
        ),
    )
    cover_letter: str = Field(
        default="",
        description="Concise personalized cover letter (plain text or markdown).",
    )


class ResumeSectionsOutput(BaseModel):
    """Retry output when the spliced resume failed verification (e.g. 2 pages)."""

    skills_section: str = Field(
        default="",
        description="Corrected full LaTeX of the \\section{Programming Skills} block.",
    )
    projects_section: str = Field(
        default="",
        description="Corrected full LaTeX of the \\section{Projects} block.",
    )
