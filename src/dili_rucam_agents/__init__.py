"""dili_rucam_agents package exposing the pipeline entry point."""


def run_end_to_end(
    pdf_path: str,
    prompt_path: str | None = None,
    output_dir: str | None = None,
    **kwargs,
) -> str:
    from .pipeline import run_end_to_end as _run_end_to_end

    return _run_end_to_end(pdf_path, prompt_path, output_dir, **kwargs)


__all__ = ["run_end_to_end"]
